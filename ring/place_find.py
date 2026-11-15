"""Place-find backend: DINOv2 heat + ALIKED/LightGlue arrival.

ORB find stays untouched. Enable with find_backend=\"place\".
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ring.trail import ARRIVED, MATCHED, UNKNOWN


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (float(np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


@dataclass
class PlaceAnchor:
    keyframe_id: int | None
    unix_ms: int
    frame_class: str
    jpeg: bytes
    embedding: np.ndarray
    path: str = ""
    offset_s: float = 0.0


@dataclass
class PlaceFindState:
    item_id: int
    name: str
    place: str
    grasp_unix_ms: int
    release_unix_ms: int
    anchors: list[PlaceAnchor]
    bg_embeddings: list[np.ndarray]
    last_seen_keyframe: int | None
    heat: float = 0.0
    heat_raw: float = 0.0
    s_item: float = 0.0
    s_bg: float = 0.0
    best_anchor_id: int | None = None
    best_anchor_jpeg_path: str = ""
    best_offset_s: float = 0.0
    status: str = UNKNOWN
    phase: str = "SEARCHING"
    guidance: str = ""
    inliers: int = 0
    match_ms: float = 0.0
    vram_mb: float | None = None
    silent: bool = False
    last_usable_at: float | None = None
    last_pulse_at: float | None = None
    arrival_hits: deque = field(default_factory=lambda: deque(maxlen=16))
    frames: int = 0
    started: float = 0.0


class LightGlueMatcher:
    """Lazy ALIKED + LightGlue matcher with timing / VRAM stats."""

    def __init__(self, max_keypoints: int = 1024) -> None:
        self.max_keypoints = max_keypoints
        self.device = "cpu"
        self.extractor = None
        self.matcher = None
        self.error = ""
        self.last_match_ms = 0.0
        self.last_vram_mb: float | None = None
        self.ready = False

    def load(self) -> None:
        if self.ready:
            return
        import torch
        from lightglue import ALIKED, LightGlue
        from lightglue.utils import rbd

        self._rbd = rbd
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.extractor = ALIKED(max_num_keypoints=self.max_keypoints).eval().to(self.device)
        self.matcher = LightGlue(features="aliked").eval().to(self.device)
        self.ready = True

    def _to_tensor(self, jpeg: bytes):
        import torch

        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("undecodable jpeg")
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        tensor = torch.from_numpy(rgb).float().permute(2, 0, 1) / 255.0
        return tensor.to(self.device)

    def match_inliers(self, live_jpeg: bytes, anchor_jpeg: bytes) -> int:
        import torch

        self.load()
        if self.device == "cuda":
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        started = time.perf_counter()
        image0 = self._to_tensor(live_jpeg)
        image1 = self._to_tensor(anchor_jpeg)
        with torch.inference_mode():
            feats0 = self.extractor.extract(image0)
            feats1 = self.extractor.extract(image1)
            matches01 = self.matcher({"image0": feats0, "image1": feats1})
        feats0, feats1, matches01 = [self._rbd(x) for x in (feats0, feats1, matches01)]
        matches = matches01["matches"]
        if matches is None or len(matches) == 0:
            inliers = 0
        else:
            # LightGlue match indices; treat as putative inliers after optional geometric check.
            pts0 = feats0["keypoints"][matches[..., 0]].detach().cpu().numpy()
            pts1 = feats1["keypoints"][matches[..., 1]].detach().cpu().numpy()
            if len(pts0) >= 8:
                _, mask = cv2.findHomography(pts0, pts1, cv2.RANSAC, 4.0)
                inliers = int(mask.sum()) if mask is not None else len(pts0)
            else:
                inliers = len(pts0)
        if self.device == "cuda":
            torch.cuda.synchronize()
            self.last_vram_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
        self.last_match_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return inliers


class PlaceFinder:
    def __init__(self, config, store, embedder) -> None:
        self.config = config
        self.store = store
        self.embedder = embedder
        self.matcher = LightGlueMatcher(max_keypoints=int(config.place_aliked_max_keypoints))
        self.state: PlaceFindState | None = None

    def close(self) -> None:
        self.state = None

    def _embed(self, jpeg: bytes) -> np.ndarray | None:
        if self.embedder is None:
            return None
        if hasattr(self.embedder, "embed_bytes"):
            return self.embedder.embed_bytes(jpeg)
        # Fallback: wait until ready and use private _embed under a simple retry.
        if not getattr(self.embedder, "ready", False):
            return None
        try:
            return self.embedder._embed(jpeg)
        except Exception:
            return None

    def build_anchors(
        self,
        *,
        item_id: int,
        name: str,
        place: str,
        grasp_unix_ms: int,
        release_unix_ms: int,
        now: float,
        extra_frames: list[dict] | None = None,
    ) -> PlaceFindState:
        cfg = self.config
        lo = int(grasp_unix_ms - cfg.place_pre_s * 1000)
        hi = int(release_unix_ms + cfg.place_post_s * 1000)
        rows = self.store.keyframes_between(lo, hi)
        anchors: list[PlaceAnchor] = []
        anchor_ids: set[int] = set()
        seen_ms: set[int] = set()
        for row in rows:
            frame_class = str(row.get("frame_class") or "")
            # Scene frames always; event frames in the span also.
            if frame_class not in ("scene", "event"):
                # If class missing (older rows), keep usable frames.
                if not row.get("usable"):
                    continue
                frame_class = "scene" if float(row.get("finger_fraction") or 0) <= 0.25 else "event"
            jpeg = self.store.jpeg(row["id"])
            if not jpeg:
                continue
            embedding = self.store.load_embedding(row["id"])
            if embedding is None:
                embedding = self._embed(jpeg)
                if embedding is not None:
                    try:
                        self.store.save_embedding(row["id"], embedding)
                    except Exception:
                        pass
            if embedding is None:
                continue
            anchors.append(
                PlaceAnchor(
                    keyframe_id=int(row["id"]),
                    unix_ms=int(row["unix_ms"]),
                    frame_class=frame_class,
                    jpeg=jpeg,
                    embedding=np.asarray(embedding, dtype=np.float32),
                    path=str(self.store.frame_dir / f"{row['id']}.jpg"),
                    offset_s=(int(row["unix_ms"]) - int(release_unix_ms)) / 1000.0,
                )
            )
            anchor_ids.add(int(row["id"]))
            seen_ms.add(int(row["unix_ms"]))

        # Live / recent frames that were never stored as keyframes still get embeddings.
        for meta in extra_frames or []:
            unix_ms = int(meta.get("unix_ms") or 0)
            jpeg = meta.get("jpeg")
            if not jpeg or unix_ms < lo or unix_ms > hi or unix_ms in seen_ms:
                continue
            frame_class = str(meta.get("frame_class") or "scene")
            if frame_class not in ("scene", "event"):
                continue
            embedding = self._embed(jpeg)
            if embedding is None:
                continue
            anchors.append(
                PlaceAnchor(
                    keyframe_id=meta.get("keyframe_id"),
                    unix_ms=unix_ms,
                    frame_class=frame_class,
                    jpeg=jpeg,
                    embedding=np.asarray(embedding, dtype=np.float32),
                    offset_s=(unix_ms - int(release_unix_ms)) / 1000.0,
                )
            )
            seen_ms.add(unix_ms)
        anchors.sort(key=lambda a: a.unix_ms)

        bg: list[np.ndarray] = []
        with self.store.lock:
            bank = self.store.db.execute(
                "SELECT id FROM keyframes WHERE bank_keyframe = 1 ORDER BY id DESC LIMIT 400"
            ).fetchall()
        for row in bank:
            kid = int(row["id"])
            if kid in anchor_ids:
                continue
            emb = self.store.load_embedding(kid)
            if emb is not None:
                bg.append(np.asarray(emb, dtype=np.float32))

        last_seen = anchors[-1].keyframe_id if anchors else None
        state = PlaceFindState(
            item_id=item_id,
            name=name,
            place=place or "",
            grasp_unix_ms=int(grasp_unix_ms),
            release_unix_ms=int(release_unix_ms),
            anchors=anchors,
            bg_embeddings=bg,
            last_seen_keyframe=last_seen,
            started=now,
            guidance=f"Place-find {name}: {len(anchors)} anchors, {len(bg)} background embeddings.",
        )
        self.state = state
        return state

    def pulse_interval(self, offset_s: float) -> float:
        """Slow tick while the match is far from the put-down. Faster as that gap shrinks."""
        cfg = self.config
        slow = float(cfg.place_pulse_cold_s)
        fast = float(cfg.find_map_min_pulse_interval)
        if not cfg.firmware_tick_command:
            fast = max(fast, 1.0)
        span = max(float(cfg.place_pre_s), float(cfg.place_post_s), 1.0)
        closeness = 1.0 - min(1.0, abs(float(offset_s)) / span)
        return slow + (fast - slow) * closeness

    def update(
        self,
        *,
        jpeg: bytes,
        usable: bool,
        unix_ms: int,
        now: float,
        live_embedding: np.ndarray | None = None,
    ) -> dict:
        state = self.state
        if state is None:
            return {}
        state.frames += 1
        if not usable or not jpeg:
            state.silent = True
            state.phase = "TURN PALM OUTWARD"
            state.guidance = "turn palm outward"
            public = self.public(now)
            public["scored"] = False
            return public

        state.last_usable_at = now
        state.silent = False
        from ring.vision import crop_finger_free_jpeg

        jpeg = crop_finger_free_jpeg(jpeg)
        embedding = live_embedding
        if embedding is None:
            embedding = self._embed(jpeg)
        if embedding is None or not state.anchors:
            state.guidance = "Waiting for embeddings…"
            public = self.public(now)
            public["scored"] = False
            return public

        sims = [cosine_similarity(embedding, a.embedding) for a in state.anchors]
        best_i = int(np.argmax(sims))
        state.s_item = float(sims[best_i])
        state.best_anchor_id = state.anchors[best_i].keyframe_id
        state.best_anchor_jpeg_path = state.anchors[best_i].path
        state.best_offset_s = float(state.anchors[best_i].offset_s)

        if state.bg_embeddings:
            bg_sims = sorted(cosine_similarity(embedding, b) for b in state.bg_embeddings)
            state.s_bg = float(np.percentile(bg_sims, 90))
        else:
            state.s_bg = 0.0

        raw = (state.s_item - state.s_bg) / float(self.config.heat_span)
        state.heat_raw = max(0.0, min(1.0, raw))
        # EMA over ~place_heat_ema_s
        tau = max(1e-3, float(self.config.place_heat_ema_s))
        # Approximate frame period from process_fps
        dt = 1.0 / max(1.0, float(self.config.process_fps))
        alpha = 1.0 - math.exp(-dt / tau)
        state.heat = (1.0 - alpha) * state.heat + alpha * state.heat_raw

        state.phase = "SEARCHING"
        state.status = UNKNOWN
        state.guidance = (
            f"Heat {state.heat:.2f} (item {state.s_item:.3f} − bg {state.s_bg:.3f}). "
            f"Look toward {state.name}."
        )

        # Arrival path
        if state.heat >= float(self.config.place_arrival_heat) and state.status != ARRIVED:
            top = sorted(
                zip(sims, state.anchors),
                key=lambda pair: pair[0],
                reverse=True,
            )[:3]
            best_inliers = 0
            try:
                for _, anchor in top:
                    inliers = self.matcher.match_inliers(jpeg, anchor.jpeg)
                    best_inliers = max(best_inliers, inliers)
                    state.match_ms = self.matcher.last_match_ms
                    state.vram_mb = self.matcher.last_vram_mb
            except Exception as error:
                state.guidance = f"LightGlue unavailable ({type(error).__name__}: {error}). Heat still guides."
                public = self.public(now)
                public["scored"] = True
                return public

            state.inliers = best_inliers
            if best_inliers >= int(self.config.place_arrival_inliers):
                state.arrival_hits.append(now)
            window = float(self.config.place_arrival_window_s)
            while state.arrival_hits and now - state.arrival_hits[0] > window:
                state.arrival_hits.popleft()
            if len(state.arrival_hits) >= int(self.config.place_arrival_fixes):
                state.status = ARRIVED
                state.phase = "ARRIVED"
                state.guidance = f"Arrived at {state.name}."
            elif best_inliers > 0:
                state.status = MATCHED
                state.phase = "LANDMARK RECOGNIZED"
                state.guidance = (
                    f"Heat {state.heat:.2f}, {best_inliers} LightGlue inliers "
                    f"({state.match_ms} ms, VRAM {state.vram_mb} MB)."
                )

        public = self.public(now)
        public["scored"] = True
        return public

    def public(self, now: float) -> dict:
        state = self.state
        if state is None:
            return {}
        return {
            "backend": "place",
            "item_id": state.item_id,
            "name": state.name,
            "place": state.place,
            "status": state.status,
            "phase": state.phase,
            "guidance": state.guidance,
            "heat": round(state.heat, 3),
            "heat_raw": round(state.heat_raw, 3),
            "s_item": round(state.s_item, 4),
            "s_bg": round(state.s_bg, 4),
            "best_anchor_keyframe": state.best_anchor_id,
            "last_seen_keyframe": state.last_seen_keyframe,
            "inliers": state.inliers,
            "match_ms": state.match_ms,
            "vram_mb": state.vram_mb,
            "silent": state.silent,
            "best_offset_s": round(state.best_offset_s, 2),
            "pulse_interval_s": round(self.pulse_interval(state.best_offset_s), 3),
            "anchor_count": len(state.anchors),
            "frames_compared": state.frames,
            "seconds": round(now - state.started, 1),
            "grasp_unix_ms": state.grasp_unix_ms,
            "release_unix_ms": state.release_unix_ms,
        }


def heat_series_for_session(
    *,
    store,
    embedder,
    config,
    grasp_unix_ms: int,
    release_unix_ms: int,
    session_start: int,
    session_end: int,
    name: str = "item",
    place: str = "",
) -> dict:
    """Offline heat curve for replay-find plots."""
    finder = PlaceFinder(config, store, embedder)
    # Ensure embedder model is loaded for sync embeds.
    if embedder is not None and not getattr(embedder, "ready", False):
        try:
            embedder._load()
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}", "times_s": [], "heat": []}
    state = finder.build_anchors(
        item_id=0,
        name=name,
        place=place,
        grasp_unix_ms=grasp_unix_ms,
        release_unix_ms=release_unix_ms,
        now=0.0,
    )
    rows = store.keyframes_between(session_start, session_end)
    times: list[float] = []
    heats: list[float] = []
    t0 = session_start
    mono = 0.0
    for row in rows:
        if not row.get("usable"):
            continue
        jpeg = store.jpeg(row["id"])
        if not jpeg:
            continue
        emb = store.load_embedding(row["id"])
        mono += 1.0 / max(1.0, float(config.process_fps))
        finder.update(
            jpeg=jpeg,
            usable=True,
            unix_ms=int(row["unix_ms"]),
            now=mono,
            live_embedding=np.asarray(emb, dtype=np.float32) if emb is not None else None,
        )
        times.append((int(row["unix_ms"]) - t0) / 1000.0)
        heats.append(finder.state.heat if finder.state else 0.0)
    return {
        "times_s": times,
        "heat": heats,
        "grasp_s": (grasp_unix_ms - t0) / 1000.0,
        "release_s": (release_unix_ms - t0) / 1000.0,
        "anchor_count": len(state.anchors),
        "bg_count": len(state.bg_embeddings),
        "ladder": [
            {"keyframe_id": a.keyframe_id, "offset_s": round(a.offset_s, 2), "unix_ms": a.unix_ms}
            for a in state.anchors
        ],
        "name": name,
        "place": place,
    }
