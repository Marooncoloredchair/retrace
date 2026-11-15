"""The continuous ReTrace loop: observe -> remember -> save events -> trail -> find -> vibration.

One Engine runs for as long as the app runs. It takes the newest camera frame
from the RingLink a few times per second and does everything in one thread, so
there is one order of events. The web server never touches this state directly;
it submits commands and reads published snapshots.

Priority order, enforced here:
  1. A Save with a valid name always becomes a persistent item (unless disk fails).
  2. Continuous observation memory keeps running even when pictures are blurry.
  3. Landmarks and Find help the wearer return; they never block remembering.
"""

from __future__ import annotations

import json
import math
import queue
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import Future
from pathlib import Path

from ring.config import FIRMWARE_PULSE_MS, Config
from ring.embedder import Embedder, cosine_distance
from ring.hold import HoldDetector
from ring.body_track import BodyTracker
from ring.mapper import Mapper, NoMapper
from ring.motor import MotorGovernor
from ring.putdown import PutDownDetector
def _pdr_click(heat: float) -> None:
    """Short laptop beep; pitch rises with heat. Best-effort, never blocks the engine."""
    import threading

    freq = int(440 + 700 * max(0.0, min(1.0, heat)))

    def _beep() -> None:
        try:
            import winsound

            winsound.Beep(freq, 60)
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True).start()


from ring.pdr import PdrTracker
from ring.place_find import PlaceFinder
from ring.vlm import VlmAnswer, VlmWorker, resolve_vlm_provider
from ring.store import (
    ANCHOR_LIMITED,
    ANCHOR_MISSING,
    ANCHOR_READY,
    CAPTURING,
    KIND_CONTEXT,
    KIND_CRUMB,
    KIND_REF,
    ROLE_LANDMARK,
    ROLE_OBSERVATION,
    TRAIL_CLOSED,
    TRAIL_RECORDING,
    Store,
)
from ring.trail import ARRIVED, MATCHED, UNKNOWN, Landmark, Reading, TrailMatcher, TrailTracker, read_frame
from ring.vision import (
    REASON_TEXT,
    analyse,
    classify_frame,
    duplicate_of,
    gyro_dps,
    landmark_ok,
    match_keypoints,
    observe_ok,
    view_blocked,
)

NAME_MAX = 40

SAVE_CAPTURING = "capturing"
SAVE_SAVED = "saved"
SAVE_FAILED = "failed"

ANCHOR_LABEL = {
    ANCHOR_READY: "VISUAL ANCHOR READY",
    ANCHOR_LIMITED: "VISUAL ANCHOR LIMITED",
    ANCHOR_MISSING: "VISUAL ANCHOR MISSING",
}


class UserError(Exception):
    """Something the person can fix. The text is shown in the dashboard as is; code names the kind of problem."""

    def __init__(self, text: str, code: str = "refused") -> None:
        super().__init__(text)
        self.code = code


def clean_name(name: str) -> str:
    text = " ".join(str(name).split())
    text = "".join(char for char in text if char.isprintable())
    if not text:
        raise UserError("Invalid item name: type a name first, for example Keys.", "invalid_name")
    if len(text) > NAME_MAX:
        raise UserError(f"Invalid item name: use at most {NAME_MAX} characters.", "invalid_name")
    return text


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store,
        link,
        clock=time.monotonic,
        wall=time.time,
        mapper: Mapper | None = None,
        echo=None,
    ) -> None:
        config.validate()
        self.config = config
        self.store = store
        self.link = link
        self.clock = clock
        self.wall = wall
        self.mapper = mapper or NoMapper()
        self.echo = echo
        find_interval = float(config.find_map_min_pulse_interval)
        if not config.firmware_tick_command:
            find_interval = max(find_interval, 1.0)
        # Outside Find the old cap stays: motor_min_interval_s and motor_max_per_minute.
        # An active map/place/pdr Find switches to find_interval and drops the per-minute cap.
        self.motor = MotorGovernor(
            send=link.request_pulse,
            clock=clock,
            min_interval_s=config.motor_min_interval_s,
            max_per_minute=config.motor_max_per_minute,
            enabled=config.motor_enabled,
            locked="" if getattr(link, "allow_writes", True) else "ReTrace was started with --no-motor",
            find_min_interval_s=find_interval,
        )
        link.use_tick_command = bool(config.firmware_tick_command)
        self.pdr = PdrTracker(config) if config.find_backend == "pdr" else None
        self.body = BodyTracker(step_length=config.body_step_length, log_dir=store.root / "body_sessions")
        self._body_pulse_at: float | None = None
        self._pdr_pending_xy: tuple[float, float] | None = None
        self._pdr_fix_queue: queue.Queue = queue.Queue()
        self.putdown = PutDownDetector(config)
        self.hold = HoldDetector(config)
        self.embedder = Embedder(
            on_embedding=self._on_embedding,
            queue_size=config.embedder_queue_size,
            enabled=config.embedder_enabled,
        )
        vlm_provider, vlm_model, vlm_warning = resolve_vlm_provider(config.vlm_provider, config.vlm_model)
        self.vlm_warning = vlm_warning
        self.vlm = VlmWorker(
            on_result=self._on_vlm_result,
            provider=vlm_provider,
            model=vlm_model,
            queue_size=config.vlm_queue_size,
        )
        self.event_windows: list[tuple[int, int]] = []  # grasp/release pin windows
        self.commands: queue.Queue = queue.Queue()
        self.remote_marks = 0
        self.pdr_run: dict | None = None
        self.camera_paused = False
        self.config_path = None
        self.pdr_report_root = None
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.started_at = clock()

        self.processed = 0
        self.observed = 0
        self.landmarks = 0
        self.usable_count = 0
        self.bank_kept = 0
        self.rejected: dict[str, int] = {}
        self.usable_rejects: dict[str, int] = {}
        self.last_frame: dict = {}
        self.recent_landmarks: deque[dict] = deque(maxlen=config.duplicate_lookback)
        self.last_observation_at: float | None = None
        self.last_landmark_at: float | None = None
        self.live_view_keyframe: int | None = None
        self.connection: int | None = None
        self.gap_pending = False
        self.imu_since_kept = {"samples": 0, "max_gyro_dps": 0.0}
        self.observation_log: deque[tuple[int, int]] = deque(maxlen=256)
        self.pin_windows: list[tuple[int, int]] = []  # (from_unix_ms, to_unix_ms) for put-down pins
        self.putdown_events_log: deque[dict] = deque(maxlen=64)
        self.hold_events_log: deque[dict] = deque(maxlen=64)
        self._recent_frame_meta: deque[dict] = deque(maxlen=400)  # ~place ±8s window + hold VLM picks
        self._pending_vlm: list[dict] = []  # hold jobs waiting for the post-release window to fill
        self.store_error = ""
        self._store_error_notice_at = -1e9
        self.capture: dict | None = None
        self.save_seq = 0
        self.save_state: dict | None = None
        self.trails: dict[int, dict] = {}
        self.find: dict | None = None
        self.notices: deque[dict] = deque(maxlen=40)
        self._usage: dict = {}
        self._usage_at = -1e9
        self._prune_at = -1e9
        self._items: list[dict] = []
        self._snapshot: dict = {}
        self._recover_items()
        self._refresh_items()
        self.publish()

    # ---- lifecycle -------------------------------------------------------------------

    def _recover_items(self) -> None:
        """Items survive a restart. An open trail does not continue across a stop."""
        for item in self.store.items():
            if item["trail_state"] in (TRAIL_RECORDING, CAPTURING):
                self.store.set_trail(item["id"], TRAIL_CLOSED, "closed when the app last stopped")
            quality = self.store.compute_anchor_quality(item["id"])
            if item.get("anchor_quality") != quality:
                self.store.set_anchor_quality(item["id"], quality)

    def start(self) -> None:
        self.embedder.start()
        self.vlm.start()
        if self.vlm_warning:
            for line in self.vlm_warning.splitlines():
                self.notice("error" if "DISABLED" in line or line.startswith("!!") else "hold", line)
            print(self.vlm_warning, flush=True)
        else:
            self.notice("hold", f"VLM ready ({self.vlm.provider} / {self.vlm.model or 'default'}).")
        self.thread = threading.Thread(target=self.run, daemon=True, name="ring-engine")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.embedder.stop()
        self.vlm.stop()
        self.motor.finding = False
        if self.find is not None:
            self._flush_find_session(self.find)
            matcher = self.find.get("matcher")
            if matcher is not None and hasattr(matcher, "close"):
                matcher.close()

    def submit(self, name: str, *args):
        if self.thread is None or not self.thread.is_alive() or threading.current_thread() is self.thread:
            return getattr(self, name)(*args)
        future: Future = Future()
        self.commands.put((name, args, future))
        return future.result(timeout=15)

    def _run_commands(self) -> None:
        while True:
            try:
                name, args, future = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                result = getattr(self, name)(*args)
            except Exception as error:
                self.publish()
                future.set_exception(error)
            else:
                self.publish()
                future.set_result(result)

    def run(self) -> None:
        after = 0
        next_process = 0.0
        period = 1.0 / self.config.process_fps
        while not self._stop.is_set():
            self._run_commands()
            wait = next_process - time.monotonic()
            if wait > 0:
                try:
                    self._ingest_imu(self.link.drain_imu())
                except Exception as error:
                    self.notice("error", f"ReTrace hit a problem and carried on: {type(error).__name__}: {error}")
                time.sleep(min(wait, 0.03))
                continue
            frame = self.link.take(after, timeout=0.1)
            try:
                if frame is not None:
                    after = frame["seq"]
                    next_process = time.monotonic() + period
                    self.process(frame)
                else:
                    self._ingest_imu(self.link.drain_imu())
                self.tick()
                self.publish()
            except Exception as error:
                self.notice("error", f"ReTrace hit a problem and carried on: {type(error).__name__}: {error}")

    # ---- notices ---------------------------------------------------------------------

    def notice(self, kind: str, text: str) -> None:
        self.notices.appendleft({"kind": kind, "text": text, "unix_ms": int(self.wall() * 1000)})
        if self.echo is not None:
            try:
                self.echo(kind, text)
            except Exception:
                pass

    def _refresh_items(self) -> None:
        self._items = self.store.items()

    def _item(self, item_id: int) -> dict:
        for item in self._items:
            if item["id"] == item_id:
                return item
        raise UserError("That item no longer exists.")

    # ---- one frame -------------------------------------------------------------------

    def _ingest_imu(self, samples: list[dict], now: float | None = None, unix_ms: int | None = None) -> None:
        """Log and step from IMU. Camera frames are not required."""
        if not samples:
            return
        if now is None:
            now = self.clock()
        if unix_ms is None:
            unix_ms = int(self.wall() * 1000)
        self.store.log_imu(samples, self.config.imu_log_file_minutes, self.config.imu_log_max_mb)
        for sample in samples:
            rotation = gyro_dps(sample)
            self.imu_since_kept["samples"] += 1
            if rotation is not None and rotation > self.imu_since_kept["max_gyro_dps"]:
                self.imu_since_kept["max_gyro_dps"] = rotation
            event = self.putdown.update(sample, now, unix_ms)
            if event is not None:
                self._record_putdown(event)
        if self.pdr is not None:
            for sample in samples:
                self.pdr.push(sample)
            self._apply_pdr_corrections()
        run = self.pdr_run
        if run is not None and run.get("phase") == "walk":
            tracker = run["tracker"]
            kept = run["samples"]
            for sample in samples:
                if sample.get("ax_g") is None or sample.get("gx_dps") is None:
                    continue
                kept.append(
                    {
                        "host_unix": float(sample.get("host_unix") or 0.0),
                        "ax_g": float(sample["ax_g"]),
                        "ay_g": float(sample["ay_g"]),
                        "az_g": float(sample["az_g"]),
                        "gx_dps": float(sample["gx_dps"]),
                        "gy_dps": float(sample["gy_dps"]),
                        "gz_dps": float(sample["gz_dps"]),
                    }
                )
                tracker.push(sample)

    def process(self, frame: dict) -> None:
        now = self.clock()
        unix_ms = int(frame["host_unix"] * 1000)
        connection = frame.get("connection")
        if connection != self.connection:
            self.gap_pending = self.connection is not None and self.last_observation_at is not None
            self.connection = connection
        self._ingest_imu(frame.get("imu_samples") or [], now=now, unix_ms=unix_ms)
        self.processed += 1
        result = analyse(
            frame["jpeg"],
            frame["seq"],
            unix_ms,
            brightness_lift=bool(getattr(self.config, "host_brightness_lift", False)),
            brightness_target_mean=float(getattr(self.config, "host_brightness_target_mean", 110.0)),
            brightness_max_gain=float(getattr(self.config, "host_brightness_max_gain", 5.0)),
        )
        if result is None:
            self._count("undecodable")
            self.last_frame = {
                "kept": False,
                "observed": False,
                "landmark": False,
                "keyframe_id": None,
                "verdict": "undecodable",
                "text": REASON_TEXT["undecodable"],
                "unix_ms": unix_ms,
            }
            return

        in_event = any(start <= unix_ms <= end for start, end in self.event_windows) or any(
            start <= unix_ms <= end for start, end in self.pin_windows
        )
        if self.hold.state == self.hold.HOLDING:
            in_event = True
        result["imu"] = frame.get("imu")
        frame_class, bank_ok, reject_reason = classify_frame(
            result, frame.get("imu"), self.config, in_event_window=in_event
        )
        usable = frame_class in ("scene", "event")
        result["usable"] = usable
        result["reject_reason"] = "" if usable else reject_reason
        result["frame_class"] = frame_class
        result["bank_eligible"] = bank_ok
        if usable:
            self.usable_count += 1
        elif reject_reason:
            self.usable_rejects[reject_reason] = self.usable_rejects.get(reject_reason, 0) + 1

        cover = float(result.get("cover_fraction", 0.0))
        from ring.vision import gyro_dps

        hold_event = self.hold.update(cover, now, unix_ms, gyro_dps(frame.get("imu")))
        if hold_event is not None:
            self._record_hold(hold_event)

        observation_id = None
        landmark_id = None
        observation_skip = observe_ok(result, self.config)
        landmark_skip = landmark_ok(result, frame.get("imu"), self.config)
        too_soon_obs = (
            self.last_observation_at is not None and now - self.last_observation_at < self.config.observation_gap_s
        )
        too_soon_landmark = (
            self.last_landmark_at is not None and now - self.last_landmark_at < self.config.min_keyframe_gap_s
        )

        if not observation_skip and not too_soon_obs:
            # After a disconnect, always store a new row so the gap is recorded on the trail.
            twin = None
            if not landmark_skip and not self.gap_pending:
                twin = duplicate_of(result, self.recent_landmarks, self.config)
            if twin is not None:
                self._count("duplicate")
                observation_id = twin["id"]
                self.live_view_keyframe = twin["id"]
            else:
                try:
                    role = ROLE_LANDMARK if not landmark_skip and not too_soon_landmark else ROLE_OBSERVATION
                    if role == ROLE_LANDMARK and too_soon_landmark:
                        role = ROLE_OBSERVATION
                    observation_id = self._store_frame(result, frame, now, unix_ms, role)
                    if role == ROLE_LANDMARK:
                        landmark_id = observation_id
                except (sqlite3.Error, OSError) as error:
                    self._store_failed(error, now)
                    self._count("storage_error")
        elif observation_skip:
            self._count(observation_skip)
        elif too_soon_obs:
            self._count("too_soon")

        if observation_id is not None:
            self.live_view_keyframe = observation_id
            self._attach_to_open_items(observation_id, is_landmark=landmark_id is not None)

        verdict = "landmark" if landmark_id is not None else "observed" if observation_id is not None else (
            observation_skip or landmark_skip or "too_soon"
        )
        self.last_frame = {
            "kept": landmark_id is not None,
            "observed": observation_id is not None,
            "landmark": landmark_id is not None,
            "keyframe_id": observation_id,
            "verdict": verdict,
            "text": REASON_TEXT.get(verdict, verdict),
            "features": result["feature_count"],
            "visible_features": result.get("visible_feature_count", result["feature_count"]),
            "sharpness": result["blur_score"],
            "coverage": result["coverage"],
            "brightness": result["brightness"],
            "contrast": result["contrast"],
            "occlusion": result.get("occlusion", 0.0),
            "finger_fraction": result.get("finger_fraction", 0.0),
            "dark_fraction": result.get("dark_fraction", 0.0),
            "frame_class": frame_class,
            "usable": result.get("usable"),
            "reject_reason": result.get("reject_reason", ""),
            "unix_ms": unix_ms,
        }
        self._recent_frame_meta.append(
            {
                "unix_ms": unix_ms,
                "sharpness": result["blur_score"],
                "jpeg": result.get("jpeg") or frame.get("jpeg"),
                "frame_class": frame_class,
                "keyframe_id": observation_id,
            }
        )

        if self.find is not None:
            if self.find.get("backend") == "pdr":
                self._find_step_pdr(now, result)
            elif self.find.get("backend") == "place":
                public = self.find.get("public") or {}
                if public.get("status") != ARRIVED:
                    self._find_step(result, now)
            elif self.find.get("backend") == "body":
                pass
            elif self.find["tracker"].status != ARRIVED:
                self._find_step(result, now)

    def _count(self, verdict: str) -> None:
        self.rejected[verdict] = self.rejected.get(verdict, 0) + 1

    def _store_failed(self, error: Exception, now: float) -> None:
        self.store_error = f"{type(error).__name__}: {error}"
        if now - self._store_error_notice_at >= 10.0:
            self._store_error_notice_at = now
            self.notice(
                "error",
                f"Filesystem error: a picture could not be written to {self.store.root} ({self.store_error}). "
                "Saved items already on disk are unchanged.",
            )

    def _imu_summary(self, frame: dict) -> dict:
        imu = frame.get("imu")
        summary = {
            "since_last_kept": {
                "samples": self.imu_since_kept["samples"],
                "max_gyro_dps": round(self.imu_since_kept["max_gyro_dps"], 1),
            }
        }
        if imu and imu.get("scaled"):
            summary["at_capture"] = {
                key: round(imu[key], 4) for key in ("ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps")
            }
            summary["board_ms"] = imu.get("sample_ms")
        return summary

    def _store_frame(self, result: dict, frame: dict, now: float, unix_ms: int, role: str) -> int:
        gap = 0.0
        if self.gap_pending and self.last_observation_at is not None:
            gap = now - self.last_observation_at
        self.gap_pending = False
        summary = self._imu_summary(frame)
        keyframe_id = self.store.add_frame(result, unix_ms, summary, role, gap)
        if any(start <= unix_ms <= end for start, end in self.pin_windows):
            self.store.pin_window(unix_ms, unix_ms)
        self.imu_since_kept = {"samples": 0, "max_gyro_dps": 0.0}
        self.observed += 1
        self.observation_log.append((unix_ms, keyframe_id))
        self.last_observation_at = now
        if role == ROLE_LANDMARK:
            self.landmarks += 1
            self.last_landmark_at = now
            visible_kp, visible_desc = match_keypoints(result)
            self.recent_landmarks.append(
                {
                    "id": keyframe_id,
                    "keypoints": result["keypoints"],
                    "descriptors": result["descriptors"],
                    "visible_keypoints": visible_kp,
                    "visible_descriptors": visible_desc,
                }
            )
        if (
            result.get("bank_eligible")
            and result.get("frame_class") == "scene"
            and not getattr(self.embedder, "hold_load", False)
        ):
            jpeg = result.get("jpeg") or frame.get("jpeg")
            if jpeg:
                self.embedder.submit(keyframe_id, jpeg, now)
        return keyframe_id

    def _on_embedding(self, keyframe_id: int, embedding, host_mono: float) -> None:
        """Called from the embedder worker. Must not block the engine loop."""
        try:
            self.store.save_embedding(keyframe_id, embedding)
            self._consider_pdr_correction(keyframe_id, embedding)
            since = int((self.wall() - self.config.keyframe_bank_window_s) * 1000)
            recent = self.store.bank_keyframes(since_unix_ms=since)
            for row in recent:
                other = self.store.load_embedding(row["id"])
                if other is None:
                    continue
                if cosine_distance(embedding, other) <= self.config.keyframe_bank_min_distance:
                    return
            self.store.mark_bank_keyframe(keyframe_id)
            self.bank_kept += 1
        except Exception as error:
            self.embedder.error = f"{type(error).__name__}: {error}"

    def _consider_pdr_correction(self, keyframe_id: int, embedding) -> None:
        """Queue a position pull when this frame matches a ladder frame near a put-down.

        Off unless pdr_visual_correction is set. The engine thread applies the pull.
        """
        if self.pdr is None or not self.config.pdr_visual_correction:
            return
        window_ms = int(float(self.config.pdr_correction_window_s) * 1000)
        max_dist = float(self.config.pdr_correction_max_distance)
        for item in self.store.items():
            if item.get("pdr_x") is None or item.get("pdr_y") is None:
                continue
            put_ms = int(item["created_unix_ms"])
            best = None
            for row in self.store.keyframes_between(put_ms - window_ms, put_ms + window_ms):
                if int(row["id"]) == int(keyframe_id):
                    continue
                other = self.store.load_embedding(row["id"])
                if other is None:
                    continue
                dist = float(cosine_distance(embedding, other))
                if best is None or dist < best:
                    best = dist
            if best is not None and best <= max_dist:
                self._pdr_fix_queue.put(
                    (int(item["id"]), float(item["pdr_x"]), float(item["pdr_y"]), round(best, 3))
                )
                return

    def _apply_pdr_corrections(self) -> None:
        if self.pdr is None:
            return
        while True:
            try:
                item_id, x, y, dist = self._pdr_fix_queue.get_nowait()
            except queue.Empty:
                return
            entry = self.pdr.correct_toward((x, y), f"item {item_id} match distance {dist}")
            if entry is None:
                return
            word = "accepted" if entry.get("accepted") else "rejected"
            self.notice("pdr", f"Visual correction {word}: {entry.get('jump_m')} m ({entry.get('reason')}).")

    def _record_hold(self, event) -> None:
        pre_ms = int(self.config.hold_pre_grasp_s * 1000)
        post_ms = int(self.config.hold_post_release_s * 1000)
        grasp_ms = event.grasp_unix_ms or event.unix_ms
        pin_from = grasp_ms - pre_ms
        pin_to = event.unix_ms + post_ms
        self.event_windows.append((pin_from, pin_to))
        self.pin_windows.append((pin_from, pin_to))
        cutoff = event.unix_ms - post_ms - 60_000
        self.event_windows = [(a, b) for a, b in self.event_windows if b >= cutoff]
        self.pin_windows = [(a, b) for a, b in self.pin_windows if b >= cutoff]
        try:
            hold_id = self.store.add_hold(
                event.unix_ms,
                event.source,
                grasp_ms,
                event.hold_ms,
                event.peak_cover,
                pin_from,
                pin_to,
            )
            pinned = self.store.pin_window(pin_from, pin_to)
        except (sqlite3.Error, OSError) as error:
            self._store_failed(error, self.clock())
            return
        entry = {
            "id": hold_id,
            "unix_ms": event.unix_ms,
            "source": event.source,
            "grasp_unix_ms": grasp_ms,
            "hold_ms": event.hold_ms,
            "peak_cover": event.peak_cover,
            "pin_from_unix_ms": pin_from,
            "pin_to_unix_ms": pin_to,
            "pinned_frames": pinned,
            "vlm_name": "",
            "vlm_description": "",
            "vlm_raw": "",
            "vlm_error": "",
            "item_id": None,
        }
        self.hold_events_log.appendleft(entry)
        label = "Marked put-down" if event.source == "marked" else "Hold released"
        self.notice("hold", f"{label} at {event.unix_ms}; pinned {pinned} frames.")
        pdr_xy = None
        if self.pdr is not None and event.source in ("hold", "marked"):
            point = self.pdr.mark("P" if event.source == "marked" else "hold")
            pdr_xy = (point["x"], point["y"])
            self._pdr_pending_xy = pdr_xy
            self.notice("pdr", f"PDR pin at ({point['x']:.2f}, {point['y']:.2f}) m, {point['steps']:.0f} steps.")
        # Passive path: put-down (auto hold or P) → VLM names → saved item. No manual Save / visual anchor.
        wait_s = 0.5 if event.source == "marked" else self.config.hold_post_release_s
        self._pending_vlm.append(
            {
                "hold_id": hold_id,
                "unix_ms": event.unix_ms,
                "grasp_unix_ms": grasp_ms,
                "pin_from": pin_from,
                "pin_to": pin_to,
                "source": event.source,
                "ready_after": self.clock() + wait_s,
                "pdr_xy": pdr_xy,
            }
        )

    def _flush_pending_vlm(self) -> None:
        now = self.clock()
        still = []
        for job in self._pending_vlm:
            if now < job["ready_after"]:
                still.append(job)
                continue
            pin_from, pin_to = job["pin_from"], job["pin_to"]
            grasp_ms = job["grasp_unix_ms"]
            pre = [
                meta
                for meta in self._recent_frame_meta
                if pin_from <= meta["unix_ms"] <= grasp_ms and meta.get("jpeg")
            ]
            for row in self.store.keyframes_between(pin_from, grasp_ms):
                jpeg = self.store.jpeg(row["id"])
                if jpeg:
                    pre.append(
                        {
                            "unix_ms": row["unix_ms"],
                            "sharpness": row["sharpness"],
                            "jpeg": jpeg,
                            "keyframe_id": row["id"],
                        }
                    )
            post = [
                meta
                for meta in self._recent_frame_meta
                if job["unix_ms"] <= meta["unix_ms"] <= pin_to and meta.get("jpeg")
            ]
            for row in self.store.keyframes_between(job["unix_ms"], pin_to):
                jpeg = self.store.jpeg(row["id"])
                if jpeg:
                    post.append(
                        {
                            "unix_ms": row["unix_ms"],
                            "sharpness": row["sharpness"],
                            "jpeg": jpeg,
                            "keyframe_id": row["id"],
                        }
                    )
            pre_sorted = sorted(pre, key=lambda m: m["sharpness"], reverse=True)[: self.config.hold_pre_frames]
            post_sorted = sorted(post, key=lambda m: m["sharpness"], reverse=True)[: self.config.hold_post_frames]
            mid: list[dict] = []
            for meta in self._recent_frame_meta:
                if grasp_ms < meta["unix_ms"] < job["unix_ms"] and meta.get("jpeg"):
                    mid.append(meta)
            for row in self.store.keyframes_between(grasp_ms + 1, job["unix_ms"] - 1):
                jpeg = self.store.jpeg(row["id"])
                if jpeg:
                    mid.append(
                        {
                            "unix_ms": row["unix_ms"],
                            "sharpness": row["sharpness"],
                            "jpeg": jpeg,
                            "keyframe_id": row["id"],
                        }
                    )
            mid_cap = int(getattr(self.config, "hold_mid_frames", 2))
            mid_sorted: list[dict] = []
            if mid and mid_cap > 0:
                mid_by_time = sorted(mid, key=lambda m: int(m["unix_ms"]))
                # Deduplicate by unix_ms then take evenly spaced samples.
                seen: set[int] = set()
                unique = []
                for item in mid_by_time:
                    key = int(item["unix_ms"])
                    if key in seen:
                        continue
                    seen.add(key)
                    unique.append(item)
                if len(unique) <= mid_cap:
                    mid_sorted = unique
                else:
                    idxs = [int(round(i * (len(unique) - 1) / (mid_cap - 1))) for i in range(mid_cap)]
                    mid_sorted = [unique[i] for i in idxs]
            jpegs = [m["jpeg"] for m in pre_sorted] + [m["jpeg"] for m in mid_sorted] + [m["jpeg"] for m in post_sorted]
            if getattr(self.config, "host_brightness_lift", False):
                from ring.vision import lift_brightness_jpeg

                target = float(getattr(self.config, "host_brightness_target_mean", 110.0))
                gain = float(getattr(self.config, "host_brightness_max_gain", 5.0))
                jpegs = [lift_brightness_jpeg(j, target_mean=target, max_gain=gain) for j in jpegs]
            last_seen = max(post_sorted, key=lambda meta: int(meta.get("unix_ms") or 0)) if post_sorted else None
            cover_id = last_seen.get("keyframe_id") if last_seen else None
            self.vlm.submit(
                {
                    "hold_id": job["hold_id"],
                    "unix_ms": job["unix_ms"],
                    "grasp_unix_ms": grasp_ms,
                    "source": job.get("source") or "hold",
                    "jpegs": jpegs,
                    "cover_keyframe": cover_id,
                    "post_meta": post_sorted,
                    "pdr_xy": job.get("pdr_xy"),
                }
            )
        self._pending_vlm = still

    def _on_vlm_result(self, job: dict, answer: VlmAnswer) -> None:
        hold_id = job.get("hold_id")
        item_id = None
        cover = job.get("cover_keyframe")
        if not cover and job.get("post_meta"):
            cover = job["post_meta"][0].get("keyframe_id")
        marked = job.get("source") == "marked"
        try:
            put_down = True if answer.put_down is None else bool(answer.put_down)
            # Passive: always save on explicit P; on auto-hold, save when VLM says put_down.
            # Fallback name keeps Find available without a manual Save / visual anchor.
            name = (answer.name or "").strip()
            place = (answer.place or answer.description or "").strip()
            should_save = False
            if marked:
                should_save = True
                if not name or answer.is_none or not put_down:
                    name = name if name and not answer.is_none else "put-down"
            elif put_down and not answer.is_none and name and not answer.error:
                should_save = True
            elif answer.error and not marked:
                self.notice("hold", f"Hold candidate unnamed ({answer.error}).")
            elif not put_down or answer.is_none:
                self.notice("hold", "Hold candidate dismissed by VLM (not a put-down).")

            if should_save and name:
                now_ms = int(job.get("unix_ms") or int(self.wall() * 1000))
                item_id = self.store.create_item(name[:40], now_ms, ANCHOR_MISSING)
                if place:
                    self.store.set_trail(item_id, TRAIL_CLOSED, place[:200])
                else:
                    self.store.set_trail(item_id, TRAIL_CLOSED, "auto-saved from put-down")
                if cover is not None:
                    self.store.append_frame(item_id, KIND_REF, int(cover))
                    self.store.append_frame(item_id, KIND_CONTEXT, int(cover))
                # Attach nearby usable frames in the put-down window as context for place-find.
                grasp_ms = int(job.get("grasp_unix_ms") or now_ms)
                attached = 0
                for row in self.store.usable_keyframes_between(
                    grasp_ms - int(self.config.place_pre_s * 1000),
                    now_ms + int(self.config.place_post_s * 1000),
                ):
                    if attached >= int(self.config.max_context_frames):
                        break
                    kid = int(row["id"])
                    if cover is not None and kid == int(cover):
                        continue
                    try:
                        self.store.append_frame(item_id, KIND_CONTEXT, kid)
                        attached += 1
                    except Exception:
                        break
                quality = self.store.compute_anchor_quality(item_id)
                self.store.set_anchor_quality(item_id, quality)
                pdr_xy = job.get("pdr_xy")
                if pdr_xy is not None:
                    self.store.set_item_position(item_id, float(pdr_xy[0]), float(pdr_xy[1]))
                self._refresh_items()
                self.notice(
                    "hold",
                    f"Saved item '{name}' from put-down"
                    + (f" @ {place}" if place else ".")
                    + " Find is ready.",
                )
            elif answer.error and marked:
                self.notice("hold", f"Put-down save failed naming ({answer.error}); check API key.")
            self.store.update_hold_vlm(
                int(hold_id),
                vlm_raw=answer.raw,
                vlm_name=answer.name or (name if should_save else ""),
                vlm_description=answer.description,
                vlm_is_none=answer.is_none,
                vlm_error=answer.error,
                item_id=item_id,
                cover_keyframe=int(cover) if cover is not None else None,
            )
            for entry in self.hold_events_log:
                if entry.get("id") == hold_id:
                    entry["vlm_name"] = answer.name
                    entry["vlm_description"] = answer.description
                    entry["vlm_raw"] = answer.raw
                    entry["vlm_error"] = answer.error
                    entry["item_id"] = item_id
                    break
        except Exception as error:
            self.vlm.error = f"{type(error).__name__}: {error}"

    def _record_putdown(self, event) -> None:
        before_ms = int(self.config.putdown_pin_before_s * 1000)
        after_ms = int(self.config.putdown_pin_after_s * 1000)
        pin_from = event.unix_ms - before_ms
        pin_to = event.unix_ms + after_ms
        self.pin_windows.append((pin_from, pin_to))
        # Drop windows that can no longer catch new frames.
        cutoff = event.unix_ms - after_ms - 60_000
        self.pin_windows = [(a, b) for a, b in self.pin_windows if b >= cutoff]
        try:
            event_id = self.store.add_putdown(
                event.unix_ms,
                event.source,
                event.peak_accel_g,
                event.stillness_ms,
                pin_from,
                pin_to,
            )
            pinned = self.store.pin_window(pin_from, pin_to)
        except (sqlite3.Error, OSError) as error:
            self._store_failed(error, self.clock())
            return
        entry = {
            "id": event_id,
            "unix_ms": event.unix_ms,
            "source": event.source,
            "peak_accel_g": event.peak_accel_g,
            "stillness_ms": event.stillness_ms,
            "pin_from_unix_ms": pin_from,
            "pin_to_unix_ms": pin_to,
            "pinned_frames": pinned,
        }
        self.putdown_events_log.appendleft(entry)
        label = "Marked put-down" if event.source == "marked" else "Put-down detected"
        self.notice("putdown", f"{label} at {event.unix_ms}; pinned {pinned} frames in the window.")

    def note_remote_mark(self) -> None:
        """The phone button reached this process. Counted before the engine queue."""
        self.remote_marks += 1

    def mark_putdown(self) -> dict:
        """Ground-truth put-down from the dashboard or keyboard shortcut (P)."""
        now = self.clock()
        unix_ms = int(self.wall() * 1000)
        from ring.putdown import PutDownEvent

        # Keep legacy putdown row for older reports, and the hold row used for precision/recall.
        imu_event = PutDownEvent(at=now, unix_ms=unix_ms, source="marked", peak_accel_g=0.0, stillness_ms=0.0)
        self._record_putdown(imu_event)
        self._record_hold(self.hold.mark(now, unix_ms))
        result = {"ok": True, "unix_ms": unix_ms, "source": "marked"}
        if self._pdr_active():
            result["pdr_test"] = self._pdr_endpoint(None)
        return result

    def _send_ring(self, data: bytes) -> None:
        send = getattr(self.link, "send_bytes", None)
        if send is not None:
            send(data)

    def _pdr_active(self) -> bool:
        run = self.pdr_run
        return bool(run) and run.get("phase") in ("settling", "walk", "need_metres")

    def _pdr_public(self) -> dict:
        run = self.pdr_run
        paused = bool(self.camera_paused)
        if not run:
            return {
                "active": False,
                "phase": "idle",
                "kind": None,
                "steps": None,
                "path_m": None,
                "walking": False,
                "closure_error_m": None,
                "imu_dropped": None,
                "drop_report": "",
                "error": "",
                "step_length": None,
                "marks": 0,
                "meters": None,
                "camera_paused": paused,
            }
        tracker = run["tracker"]
        frozen_steps = run.get("steps_frozen")
        steps = float(tracker.steps) if frozen_steps is None else float(frozen_steps)
        return {
            "active": run.get("phase") in ("settling", "walk", "need_metres"),
            "phase": run.get("phase"),
            "kind": run.get("kind"),
            "steps": round(steps, 2),
            "path_m": round(float(tracker.path_m), 2),
            "walking": bool(tracker.walking),
            "closure_error_m": run.get("closure_error_m"),
            "imu_dropped": run.get("imu_dropped"),
            "drop_report": run.get("drop_report") or "",
            "error": run.get("error") or "",
            "step_length": run.get("step_length"),
            "marks": len(tracker.marks),
            "meters": run.get("meters"),
            "camera_paused": paused,
        }

    def _note_imu_drops(self) -> None:
        """Remember each new gap against the latest Start or End tap, and whether the gate was open."""
        from ring.pdr_live import describe_drops

        run = self.pdr_run
        if run is None or run.get("phase") not in ("settling", "walk", "need_metres", "done"):
            return
        status = self.link.status()
        dropped = int((status.get("imu") or {}).get("dropped") or 0)
        previous = int(run.get("dropped_seen") if run.get("dropped_seen") is not None else dropped)
        tracker = run.get("tracker")
        phase = run.get("phase")
        walking_now = bool(getattr(tracker, "walking", False)) and phase != "done"
        if dropped > previous:
            gate_open = bool(walking_now or run.get("gate_was_open")) if phase != "done" else False
            run.setdefault("drops", []).append(
                {"count": dropped - previous, "unix": self.wall(), "gate_open": gate_open}
            )
        if phase != "done":
            run["gate_was_open"] = walking_now
        run["dropped_seen"] = dropped
        run["imu_dropped"] = max(0, dropped - int(run.get("dropped_start") or 0))
        run["drop_report"] = describe_drops(run.get("drops") or [], run.get("taps") or [])

    def _pdr_settle(self) -> None:
        """Show the walk instruction only after the rate has stayed at or above 80 Hz for one second."""
        from ring.pdr_live import PREFLIGHT_MIN_HZ, imu_rate_hz

        run = self.pdr_run
        if not run or run.get("phase") != "settling":
            return
        now = self.clock()
        if imu_rate_hz(self.link) >= PREFLIGHT_MIN_HZ:
            since = run.get("steady_since")
            if since is None:
                run["steady_since"] = now
            elif now - float(since) >= 1.0:
                run["phase"] = "walk"
                run["steady_since"] = None
                if run.get("kind") == "calib":
                    text = "Walk a known distance, type the metres, and press End."
                else:
                    text = "Stand on the start point and press End, then walk the loop."
                self.notice("pdr", text)
        else:
            run["steady_since"] = None

    def _notice_drops(self, run: dict) -> None:
        report = str(run.get("drop_report") or "")
        if report:
            self.notice("pdr", report)

    def pdr_start(self, kind: str) -> dict:
        """Begin calibration or a loop inside this process. Pictures pause. The model stays unloaded."""
        kind = str(kind or "").strip().lower()
        if kind not in ("calib", "swing", "carry"):
            raise UserError("PDR test must be calibration, swing, or carry.")
        if self._pdr_active():
            raise UserError("A PDR test is already running. Press End first.")
        self._send_ring(b"i")
        self.camera_paused = True
        self.embedder.hold_load = True
        from ring.net import hotspot_up
        from ring.pdr_live import start_block

        error = start_block(self.link, hotspot=hotspot_up())
        if error:
            if not self.config.pdr_session_keep_camera_paused:
                self._send_ring(b"c")
                self.camera_paused = False
                self.embedder.hold_load = False
            self.notice("error", error)
            raise UserError(error, "ring_not_connected")
        tracker = PdrTracker(self.config)
        tracker.frozen = False
        status = self.link.status()
        dropped = int((status.get("imu") or {}).get("dropped") or 0)
        self.pdr_run = {
            "kind": kind,
            "phase": "settling",
            "tracker": tracker,
            "samples": [],
            "p_unix": [],
            "taps": [{"name": "start", "unix": self.wall()}],
            "drops": [],
            "dropped_start": dropped,
            "dropped_seen": dropped,
            "gate_was_open": False,
            "steady_since": None,
            "imu_dropped": 0,
            "drop_report": "No IMU samples were dropped.",
            "closure_error_m": None,
            "error": "",
            "step_length": None,
            "meters": None,
            "steps_frozen": None,
        }
        self.notice(
            "pdr",
            "Pictures paused. Waiting until the motion stream stays above 80 Hz for one second.",
        )
        self.publish()
        return self._pdr_public()

    def pdr_end(self, meters: float | None = None) -> dict:
        if not self._pdr_active():
            raise UserError("No PDR test is running.")
        return self._pdr_endpoint(meters)

    def pdr_cancel(self) -> dict:
        run = self.pdr_run
        if not run or run.get("phase") not in ("settling", "walk", "need_metres"):
            return self._pdr_public()
        run["tracker"].frozen = True
        self._finish_camera(run)
        run["phase"] = "done"
        run["error"] = "Cancelled. Step length was not changed."
        self.notice("pdr", run["error"])
        self._notice_drops(run)
        self.publish()
        return self._pdr_public()

    def pdr_resume_camera(self) -> dict:
        """Turn pictures back on. Refused while a test is still in progress."""
        if self._pdr_active():
            raise UserError("A PDR test is still running. Press End first.")
        self._send_ring(b"c")
        self.camera_paused = False
        self.embedder.hold_load = False
        self.notice("pdr", "Camera resumed.")
        self.publish()
        return self._pdr_public()

    def _finish_camera(self, run: dict) -> None:
        self._note_imu_drops()
        if self.config.pdr_session_keep_camera_paused:
            self.camera_paused = True
            self.embedder.hold_load = True
            return
        self._send_ring(b"c")
        self.camera_paused = False
        self.embedder.hold_load = False
        self._note_imu_drops()

    def _write_step_length(self, length: float) -> float:
        from pathlib import Path

        from ring.pdr_live import write_step_length

        path = self.config_path
        if path is None:
            from ring.service import CONFIG_PATH

            path = CONFIG_PATH
        return write_step_length(Path(path), length)

    def _write_loop_report(self, run: dict) -> None:
        from pathlib import Path

        from ring.pdr_live import write_loop_report

        root = self.pdr_report_root
        if root is None:
            root = Path(__file__).resolve().parents[1] / "reports"
        try:
            write_loop_report(Path(root), run)
        except Exception as error:
            self.notice("error", f"Loop report was not written: {type(error).__name__}: {error}")

    def _pdr_endpoint(self, meters: float | None) -> dict:
        run = self.pdr_run
        if run is None or run.get("phase") not in ("settling", "walk", "need_metres"):
            raise UserError("No PDR test is running.")
        self._note_imu_drops()
        run.setdefault("taps", []).append({"name": "end", "unix": self.wall()})
        tracker: PdrTracker = run["tracker"]
        if run["kind"] == "calib":
            if run.get("steps_frozen") is None:
                tracker.frozen = True
                run["steps_frozen"] = float(tracker.steps)
            steps = float(run["steps_frozen"])
            if meters is None or not math.isfinite(float(meters)) or float(meters) <= 0:
                run["phase"] = "need_metres"
                run["error"] = "Type the distance in metres, then press End."
                self.publish()
                return self._pdr_public()
            meters_f = float(meters)
            if steps < 1:
                run["meters"] = meters_f
                run["error"] = (
                    f"The ring counted {steps:.1f} steps over {meters_f:.1f} m, so the step length was not changed."
                )
                self._finish_camera(run)
                run["phase"] = "done"
                self.notice("pdr", run["error"])
                self._notice_drops(run)
                self.publish()
                return self._pdr_public()
            try:
                length = self._write_step_length(meters_f / steps)
            except Exception as error:
                run["error"] = f"Step length was not saved ({type(error).__name__}: {error})."
                self._finish_camera(run)
                run["phase"] = "done"
                self.notice("error", run["error"])
                self._notice_drops(run)
                self.publish()
                return self._pdr_public()
            self.config.pdr_step_length = length
            run["step_length"] = length
            run["meters"] = meters_f
            run["error"] = ""
            self._finish_camera(run)
            run["phase"] = "done"
            self.notice("pdr", f"Step length {length:.3f} m from {steps:.1f} steps over {meters_f:.2f} m.")
            self._notice_drops(run)
            self.publish()
            return self._pdr_public()
        if len(tracker.marks) == 0:
            tracker.mark("P")
            run["p_unix"].append(self.wall())
            self.notice("pdr", "Loop start marked. Walk back to the same point and press End.")
            self.publish()
            return self._pdr_public()
        tracker.frozen = True
        tracker.mark("P")
        run["p_unix"].append(self.wall())
        first, second = tracker.marks[0], tracker.marks[1]
        closure = float(math.hypot(first["x"] - second["x"], first["y"] - second["y"]))
        run["closure_error_m"] = round(closure, 3)
        run["error"] = ""
        self._finish_camera(run)
        run["phase"] = "done"
        self._write_loop_report(run)
        self.notice(
            "pdr",
            f"Loop closed. Steps {tracker.steps:.1f}, path {tracker.path_m:.2f} m, closure {closure:.2f} m.",
        )
        self._notice_drops(run)
        self.publish()
        return self._pdr_public()

    def _attach_to_open_items(self, keyframe_id: int, is_landmark: bool) -> None:
        if self.capture is not None and self.capture.get("item_id") is not None:
            self._capture_add(keyframe_id, is_landmark=is_landmark)
        now = self.clock()
        for item_id in list(self.trails):
            trail = self.trails[item_id]
            # Prefer landmarks as breadcrumbs; also take spaced observations so the trail
            # keeps growing when the wearer walks with a partly covered lens.
            last_crumb_at = trail.get("last_crumb_at")
            take = is_landmark
            if not take and (last_crumb_at is None or now - last_crumb_at >= self.config.crumb_gap_s):
                row = self.store.keyframe(keyframe_id)
                take = row is not None and row.get("features", 0) >= 1
            if not take:
                continue
            position = self.store.append_frame(item_id, KIND_CRUMB, keyframe_id)
            trail["count"] = position
            trail["last_crumb_at"] = now
            quality = self.store.compute_anchor_quality(item_id)
            self.store.set_anchor_quality(item_id, quality)
            if position >= self.config.trail_max_breadcrumbs:
                self._close_trail(item_id, f"full at {position} breadcrumbs")
        if self.trails or self.capture is not None:
            self._refresh_items()

    # ---- save item -------------------------------------------------------------------

    def save_item(self, name: str) -> dict:
        try:
            return self._save_start(name)
        except UserError as error:
            self.notice("error", f"Save refused. {error}")
            raise

    def _save_start(self, name: str) -> dict:
        name = clean_name(name)
        if self.capture is not None:
            raise UserError(f"Still saving {self.capture['name']}. Wait until that has finished.", "busy")
        if len(self._items) >= self.config.max_items:
            raise UserError(f"There are already {self.config.max_items} saved items. Delete one first.", "too_many_items")

        now = self.clock()
        now_ms = int(self.wall() * 1000)
        self.save_seq += 1
        request = self.save_seq

        try:
            lookback = now_ms - int(self.config.save_lookback_s * 1000)
            recent = self.store.keyframes_since(lookback)
            if self.live_view_keyframe is not None and all(frame["id"] != self.live_view_keyframe for frame in recent):
                live = self.store.keyframe(self.live_view_keyframe)
                if live is not None:
                    recent.append(live)
            # Prefer more recent frames, keep a bounded set.
            recent = sorted(recent, key=lambda frame: frame["id"])[-self.config.max_context_frames :]
            landmarks = [frame for frame in recent if frame.get("role") == ROLE_LANDMARK]
            first_refs = landmarks[-max(1, self.config.max_reference_frames // 2) :] if landmarks else []
            item_id = self.store.create_item(name, now_ms, ANCHOR_MISSING)
            for frame in recent:
                self.store.append_frame(item_id, KIND_CONTEXT, frame["id"])
            for frame in first_refs:
                self.store.append_frame(item_id, KIND_REF, frame["id"])
            quality = self.store.compute_anchor_quality(item_id)
            self.store.set_anchor_quality(item_id, quality)
        except (sqlite3.Error, OSError) as error:
            text = self._storage_text(error)
            self.save_state = {
                "request": request,
                "name": name,
                "state": SAVE_FAILED,
                "code": "storage_error",
                "text": text,
                "item_id": None,
                "references": 0,
                "context": 0,
                "anchor_quality": ANCHOR_MISSING,
                "unix_ms": now_ms,
            }
            self.notice("error", text)
            raise UserError(text, "storage_error") from None

        self.capture = {
            "request": request,
            "item_id": item_id,
            "name": name,
            "until": now + self.config.save_capture_s,
            "references": len(first_refs),
            "context": len(recent),
        }
        self._refresh_items()
        label = ANCHOR_LABEL[quality]
        if quality == ANCHOR_READY:
            advice = "Visual anchor ready. Slowly look around the spot while ReTrace records more."
        elif quality == ANCHOR_LIMITED:
            advice = (
                "Saved with limited visual context. ReTrace will keep recording; point the camera at the "
                "surroundings when you can. Find will not claim a location until a stronger anchor exists."
            )
        else:
            advice = (
                "Saved with no reliable visual anchor yet. The Save event is remembered. "
                "Point the camera at the room as you walk away so breadcrumbs can form."
            )
        fix = self.body.has_fix()
        if fix:
            self.store.set_item_position(item_id, self.body.x, self.body.y, self.body.frame_id)
            self._refresh_items()
            advice = f"{advice} Phone position stored ({self.body.x:.1f} m east, {self.body.y:.1f} m north)."
        else:
            advice = f"{advice} No phone position yet, so Find has no distance for this item."
        text = f"{name} saved. {label}. {advice}"
        self._save_set(SAVE_CAPTURING, "", text, quality)
        self.notice("saved", text)
        return self._save_public(now)

    def _storage_text(self, error) -> str:
        detail = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
        return (
            f"Nothing was saved. Filesystem error: ReTrace could not write to {self.store.root} "
            f"({detail}). Check free disk space and that the folder can be written."
        )

    def _save_set(self, state: str, code: str, text: str, quality: str | None = None) -> None:
        capture = self.capture
        item_id = None if capture is None else capture["item_id"]
        if quality is None and item_id is not None:
            quality = self.store.compute_anchor_quality(item_id)
        self.save_state = {
            "request": 0 if capture is None else capture["request"],
            "name": "" if capture is None else capture["name"],
            "state": state,
            "code": code,
            "text": text,
            "item_id": item_id,
            "references": 0 if capture is None else capture["references"],
            "context": 0 if capture is None else capture.get("context", 0),
            "anchor_quality": quality or ANCHOR_MISSING,
            "unix_ms": int(self.wall() * 1000),
        }

    def _save_public(self, now: float) -> dict | None:
        if self.save_state is None:
            return None
        state = dict(self.save_state)
        capture = self.capture
        if capture is not None:
            state["references"] = capture["references"]
            state["context"] = capture.get("context", 0)
            state["seconds_left"] = round(max(0.0, capture["until"] - now), 1)
            if capture["item_id"] is not None:
                state["anchor_quality"] = self.store.compute_anchor_quality(capture["item_id"])
        return state

    def _capture_add(self, keyframe_id: int, is_landmark: bool) -> None:
        capture = self.capture
        if capture is None or capture["item_id"] is None:
            return
        item_id = capture["item_id"]
        self.store.append_frame(item_id, KIND_CONTEXT, keyframe_id)
        capture["context"] = len(self.store.item_frame_ids(item_id, KIND_CONTEXT))
        if is_landmark and capture["references"] < self.config.max_reference_frames:
            self.store.append_frame(item_id, KIND_REF, keyframe_id)
            capture["references"] = len(self.store.item_frame_ids(item_id, KIND_REF))
        quality = self.store.compute_anchor_quality(item_id)
        self.store.set_anchor_quality(item_id, quality)
        self._refresh_items()

    def _capture_finish(self) -> None:
        capture = self.capture
        if capture is None or capture["item_id"] is None:
            self.capture = None
            return
        item_id = capture["item_id"]
        quality = self.store.compute_anchor_quality(item_id)
        self.store.set_anchor_quality(item_id, quality)
        self.store.set_trail(item_id, TRAIL_RECORDING)
        self.trails[item_id] = {"started": self.clock(), "count": 0, "last_crumb_at": None}
        self._refresh_items()
        text = (
            f"{capture['name']} saved with {capture['references']} visual anchor frame(s) and "
            f"{capture.get('context', 0)} context frame(s). {ANCHOR_LABEL[quality]}. "
            "Breadcrumbs are now being recorded as you move."
        )
        self._save_set(SAVE_SAVED, "", text, quality)
        self.capture = None
        self.notice("saved", text)

    def _close_trail(self, item_id: int, note: str) -> None:
        if self.trails.pop(item_id, None) is None:
            return
        self.store.set_trail(item_id, TRAIL_CLOSED, note)
        self._refresh_items()
        try:
            self.notice("trail", f"Breadcrumb trail for {self._item(item_id)['name']} closed: {note}.")
        except UserError:
            pass

    def stop_trail(self, item_id: int) -> dict:
        self._item(item_id)
        if item_id not in self.trails:
            raise UserError("That item's breadcrumb trail is not recording.")
        self._close_trail(item_id, "stopped by you")
        return {"item_id": item_id}

    def delete_item(self, item_id: int) -> dict:
        item = self._item(item_id)
        if self.capture is not None and self.capture["item_id"] == item_id:
            self._save_set(SAVE_FAILED, "deleted", f"{item['name']} was deleted while it was being saved.")
            self.capture = None
        self.trails.pop(item_id, None)
        if self.find is not None and self.find["item_id"] == item_id:
            self.stop_find()
        self.store.delete_item(item_id)
        self._refresh_items()
        self.notice("deleted", f"Deleted {item['name']}. Its pictures return to the rolling memory and its limits.")
        return {"item_id": item_id}

    def item_detail(self, item_id: int) -> dict:
        item = self._item(item_id)
        frames = []
        for stage, keyframe_id in enumerate(item["breadcrumbs"], start=1):
            row = self.store.keyframe(keyframe_id) or {}
            frames.append(
                {
                    "stage": stage,
                    "keyframe_id": keyframe_id,
                    "unix_ms": row.get("unix_ms"),
                    "gap_before_s": row.get("gap_before_s", 0),
                    "role": row.get("role"),
                    "occlusion": row.get("occlusion", 0),
                }
            )
        return {
            "id": item["id"],
            "name": item["name"],
            "created_unix_ms": item["created_unix_ms"],
            "references": item["references"],
            "context": item.get("context", []),
            "breadcrumbs": frames,
            "trail_state": item["trail_state"],
            "trail_note": item["trail_note"],
            "anchor_quality": item.get("anchor_quality", ANCHOR_MISSING),
        }

    # ---- find item -------------------------------------------------------------------

    def _item_hold_window(self, item_id: int) -> tuple[int, int, str]:
        """Return (grasp_unix_ms, release_unix_ms, place) for place-find anchors."""
        place = ""
        for event in reversed(self.store.hold_events()):
            if event.get("item_id") == item_id:
                grasp = int(event.get("grasp_unix_ms") or event["unix_ms"])
                release = int(event["unix_ms"])
                place = str(event.get("vlm_description") or event.get("vlm_name") or "")
                return grasp, release, place
        item = self._item(item_id)
        created = int(item.get("created_unix_ms") or int(self.wall() * 1000))
        return created, created, str(item.get("trail_note") or "")

    def start_find(self, item_id: int) -> dict:
        item = self._item(item_id)
        if self.capture is not None and self.capture["item_id"] == item_id:
            raise UserError(f"Still saving {item['name']}. Wait a few seconds.")
        if item_id in self.trails:
            self._close_trail(item_id, "closed when Find started")
            item = self._item(item_id)
        if self.find is not None:
            self.stop_find()

        if self.config.find_backend == "place":
            try:
                return self._start_find_place(item_id, item)
            except UserError as error:
                if error.code == "anchor_missing" and self._body_pin_current(item):
                    return self._start_find_body(item_id, item)
                if error.code == "anchor_missing" and item.get("pdr_x") is not None:
                    raise UserError(
                        "That pin is from an earlier session. Stand at the object and press Save again.",
                        "stale_frame",
                    ) from error
                raise
        if self.config.find_backend == "pdr":
            return self._start_find_pdr(item_id, item)

        quality = item.get("anchor_quality", ANCHOR_MISSING)
        landmarks = []
        for keyframe_id in item["references"]:
            keypoints, descriptors = self.store.features(keyframe_id)
            if descriptors is not None:
                landmarks.append(Landmark(0, keyframe_id, keypoints, descriptors))
        missing = 0
        for stage, keyframe_id in enumerate(item["breadcrumbs"], start=1):
            keypoints, descriptors = self.store.features(keyframe_id)
            if descriptors is None:
                missing += 1
                continue
            landmarks.append(Landmark(stage, keyframe_id, keypoints, descriptors))
        if not landmarks:
            raise UserError(
                f"{item['name']} has no matchable pictures yet ({ANCHOR_LABEL.get(quality, quality)}). "
                "The Save event is remembered, but Find cannot guide you until ReTrace records a clearer view "
                "or some breadcrumbs. Point the camera at the surroundings and try again.",
                "anchor_missing",
            )
        matcher = TrailMatcher(landmarks, self.config)
        self.find = {
            "backend": "orb",
            "item_id": item_id,
            "name": item["name"],
            "anchor_quality": quality,
            "started": self.clock(),
            "matcher": matcher,
            "tracker": TrailTracker(
                breadcrumbs=len(item["breadcrumbs"]), config=self.config, neighbours=matcher.neighbours
            ),
            "stages": (
                [item["references"][0], *item["breadcrumbs"]]
                if item["references"]
                else [(item.get("context") or [None])[0], *item["breadcrumbs"]]
                if item.get("context")
                else list(item["breadcrumbs"])
            ),
            "references": list(item["references"]),
            "frames": 0,
            "blocked_frames": 0,
            "match_ms": 0.0,
            "ended": "",
            "feedback": [],
            "guidance": "",
        }
        text = (
            f"Finding {item['name']}: {ANCHOR_LABEL.get(quality, quality)}, "
            f"{len(item['references'])} anchor frame(s), {len(item['breadcrumbs'])} breadcrumb(s)."
        )
        if quality != ANCHOR_READY:
            text += " Arrival will only be declared if a saved-spot picture is recognised later."
        if missing:
            text += f" {missing} breadcrumb file(s) are missing and were skipped."
        self.find["guidance"] = text
        self.notice("find", text)
        self._open_find_session(self.find)
        return {
            "item_id": item_id,
            "name": item["name"],
            "breadcrumbs": len(item["breadcrumbs"]),
            "anchor_quality": quality,
        }

    def _start_find_place(self, item_id: int, item: dict) -> dict:
        grasp, release, place = self._item_hold_window(item_id)
        finder = PlaceFinder(self.config, self.store, self.embedder)
        now = self.clock()
        lo = int(grasp - self.config.place_pre_s * 1000)
        hi = int(release + self.config.place_post_s * 1000)
        extra = [
            meta
            for meta in self._recent_frame_meta
            if meta.get("jpeg") and lo <= int(meta["unix_ms"]) <= hi
        ]
        state = finder.build_anchors(
            item_id=item_id,
            name=item["name"],
            place=place or str(item.get("trail_note") or ""),
            grasp_unix_ms=grasp,
            release_unix_ms=release,
            now=now,
            extra_frames=extra,
        )
        if not state.anchors:
            raise UserError(
                f"{item['name']} has no scene/event frames in the ±{self.config.place_pre_s:.0f}s put-down window. "
                "Wear and put down again, or use find_backend=orb.",
                "anchor_missing",
            )
        self.find = {
            "backend": "place",
            "item_id": item_id,
            "name": item["name"],
            "place": state.place,
            "anchor_quality": item.get("anchor_quality", ANCHOR_MISSING),
            "started": now,
            "place_finder": finder,
            "matcher": finder,  # stop_find calls .close()
            "tracker": None,
            "stages": [a.keyframe_id for a in state.anchors if a.keyframe_id],
            "references": list(item.get("references") or []),
            "frames": 0,
            "blocked_frames": 0,
            "match_ms": 0.0,
            "ended": "",
            "feedback": [],
            "guidance": state.guidance,
            "public": finder.public(now),
        }
        self.notice("find", state.guidance)
        self._open_find_session(self.find)
        return {
            "item_id": item_id,
            "name": item["name"],
            "backend": "place",
            "anchors": len(state.anchors),
            "place": state.place,
        }

    def _start_find_pdr(self, item_id: int, item: dict) -> dict:
        if self.pdr is None:
            raise UserError("PDR is not running. Set find_backend to \"pdr\" and restart.")
        target = (item.get("pdr_x"), item.get("pdr_y"))
        if target[0] is None or target[1] is None:
            raise UserError(
                f"{item['name']} has no PDR coordinate yet. Put it down (press P) while this backend is running."
            )
        self.find = {
            "backend": "pdr",
            "item_id": item_id,
            "name": item["name"],
            "place": str(item.get("trail_note") or ""),
            "target": (float(target[0]), float(target[1])),
            "last_seen": (item.get("references") or [None])[-1] if item.get("references") else None,
            "started": self.clock(),
            "matcher": None,
            "tracker": None,
            "frames": 0,
            "blocked_frames": 0,
            "match_ms": 0.0,
            "ended": "",
            "feedback": [],
            "guidance": f"Walking toward {item['name']}.",
            "public": {},
            "anchor_quality": item.get("anchor_quality"),
            "references": list(item.get("references") or []),
            "stages": [],
        }
        self.notice("find", self.find["guidance"])
        self._open_find_session(self.find)
        return {"item_id": item_id, "name": item["name"], "backend": "pdr"}

    def _find_step_pdr(self, now: float, result: dict | None = None) -> None:
        find = self.find
        tracker = self.pdr
        if tracker is None or find is None:
            return
        tx, ty = find["target"]
        dx = float(tracker.pos[0]) - tx
        dy = float(tracker.pos[1]) - ty
        distance = math.hypot(dx, dy)
        span = max(float(self.config.pdr_heat_range_m), 1e-3)
        heat = max(0.0, min(1.0, 1.0 - distance / span))
        close = distance <= float(self.config.pdr_close_m)
        phase = "close" if close else "SEARCHING"
        guidance = (
            f"{distance:.1f} m from {find['name']}"
            + (" — look around, you are close." if close else ".")
        )
        cold = float(self.config.place_pulse_cold_s)
        hot = float(self.config.find_map_min_pulse_interval)
        if not self.config.firmware_tick_command:
            hot = max(hot, 1.0)
        interval = cold + (hot - cold) * heat
        public = {
            "backend": "pdr",
            "status": "close" if close else "SEARCHING",
            "phase": phase,
            "guidance": guidance,
            "heat": round(heat, 3),
            "distance_m": round(distance, 2),
            "name": find["name"],
            "place": find.get("place") or "",
            "last_seen_keyframe": find.get("last_seen") if close else None,
            "matched_keyframe": find.get("last_seen") if close else None,
            "pulse_interval_s": round(interval, 2),
            "inliers": 0,
            "silent": False,
        }
        find["public"] = public
        find["guidance"] = guidance
        from ring.vision import find_frame_ok

        passed = bool(result is not None and find_frame_ok(result, result.get("imu"), self.config))
        unix_ms = int((result or {}).get("capture_unix_ms") or int(self.wall() * 1000))
        self._log_find_frame(find, unix_ms=unix_ms, passed_gate=passed, s_item=None, s_bg=None, heat=round(heat, 3))
        last = find.get("last_pulse_at")
        if last is not None and now - last < interval:
            self._note_find_request(find, {"sent": False, "blocked": "", "coalesced": True})
            return
        outcome = self.motor.request("guide", f"{find['name']} {distance:.1f} m")
        if not outcome.get("coalesced"):
            find["feedback"].append(outcome)
        self._note_find_request(find, outcome)
        find["last_pulse_at"] = now
        if outcome.get("sent"):
            _pdr_click(heat)

    def stop_find(self) -> dict:
        find, self.find = self.find, None
        self.motor.finding = False
        if find is None:
            return {"stopped": False}
        self._flush_find_session(find)
        matcher = find.get("matcher")
        if matcher is not None and hasattr(matcher, "close"):
            matcher.close()
        if not find["ended"]:
            self.notice("find", f"Stopped finding {find['name']}.")
        return {"stopped": True}

    def _open_find_session(self, find: dict) -> None:
        """One log for this Find: pulse counts plus one row per processed frame."""
        started_ms = int(self.wall() * 1000)
        folder = self.store.root / "find_sessions"
        folder.mkdir(parents=True, exist_ok=True)
        find["session"] = {"requested": 0, "sent": 0, "blocked": 0, "frames": []}
        find["session_started_unix_ms"] = started_ms
        find["session_path"] = str(folder / f"{started_ms}-{find['item_id']}.json")
        # Trail Find (orb) keeps the 3 s / per-minute governor. The tests lock that in.
        # map / place / pdr already speak the 'v' tick, so that Find drops the per-minute cap.
        self.motor.finding = self.config.find_backend in ("map", "place", "pdr") or find.get("backend") == "body"
        self._flush_find_session(find)

    def _log_find_frame(
        self,
        find: dict,
        *,
        unix_ms: int,
        passed_gate: bool,
        s_item: float | None,
        s_bg: float | None,
        heat: float | None,
    ) -> None:
        session = find.get("session")
        if session is None:
            return
        session["frames"].append(
            {
                "unix_ms": int(unix_ms),
                "passed_gate": bool(passed_gate),
                "s_item": s_item,
                "s_bg": s_bg,
                "heat": heat,
            }
        )
        self._flush_find_session(find)

    def _note_find_request(self, find: dict, outcome: dict) -> None:
        session = find.get("session")
        if session is None:
            return
        session["requested"] += 1
        if outcome.get("sent"):
            session["sent"] += 1
        elif outcome.get("blocked"):
            session["blocked"] += 1
        self._flush_find_session(find)

    def _flush_find_session(self, find: dict) -> None:
        path = find.get("session_path")
        session = find.get("session")
        if not path or session is None:
            return
        payload = {
            "item_id": find.get("item_id"),
            "name": find.get("name"),
            "backend": find.get("backend"),
            "started_unix_ms": find.get("session_started_unix_ms"),
            "requested": session["requested"],
            "sent": session["sent"],
            "blocked": session["blocked"],
            "target_x": find.get("target_x"),
            "target_y": find.get("target_y"),
            "track": session.get("track") or [],
            "frames": session["frames"],
        }
        try:
            target = Path(path)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(target)
        except OSError as error:
            if not find.get("session_log_error"):
                find["session_log_error"] = True
                self.notice("error", f"Find session log was not written: {type(error).__name__}: {error}")

    def _note_body_track(self, now: float, guide: dict | None) -> None:
        find = self.find
        if find is None or find.get("backend") != "body":
            return
        session = find.get("session")
        if session is None:
            return
        track = session.setdefault("track", [])
        if track and now - float(track[-1].get("t") or 0) < 0.4:
            return
        heading = self.body.heading
        track.append(
            {
                "t": round(now, 3),
                "x": round(self.body.x, 3),
                "y": round(self.body.y, 3),
                "heading": None if heading is None else round(heading, 1),
                "distance_m": None if guide is None else guide.get("distance_m"),
                "bearing": None if guide is None else guide.get("bearing"),
                "error_deg": None if guide is None else guide.get("error_deg"),
            }
        )
        if len(track) > 2000:
            del track[:-1500]
        if len(track) % 4 == 0:
            self._flush_find_session(find)

    def body_live(self) -> dict:
        now = self.clock()
        find = self.find
        public = None
        if find is not None and find.get("backend") == "body":
            public = self._find_public(now)
        return {
            "body": self.body.public(now),
            "items": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "x": item.get("pdr_x"),
                    "y": item.get("pdr_y"),
                    "body_frame": item.get("body_frame"),
                }
                for item in self._items
            ],
            "find": public,
        }

    def body_replay(self, kind: str) -> bytes:
        from ring.body_replay import render_replay

        track, pin, name = self._replay_source()
        png, gif = render_replay(track, pin, name)
        if kind == "gif":
            return gif
        return png

    def _replay_source(self) -> tuple[list, tuple[float, float] | None, str]:
        find = self.find
        if find is not None and find.get("backend") == "body" and (find.get("session") or {}).get("track"):
            pin = None
            if find.get("target_x") is not None:
                pin = (float(find["target_x"]), float(find["target_y"]))
            return list(find["session"]["track"]), pin, str(find.get("name") or "item")
        folder = self.store.root / "find_sessions"
        if folder.is_dir():
            files = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
            for path in files:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                track = payload.get("track") or []
                if payload.get("backend") != "body" or not track:
                    continue
                pin = None
                if payload.get("target_x") is not None and payload.get("target_y") is not None:
                    pin = (float(payload["target_x"]), float(payload["target_y"]))
                return track, pin, str(payload.get("name") or "item")
        raise UserError("No pocket Find has been logged yet. Press Find, walk, then save the replay.")

    def _session_public(self, find: dict) -> dict:
        session = find.get("session") or {}
        return {
            "requested": int(session.get("requested") or 0),
            "sent": int(session.get("sent") or 0),
            "blocked": int(session.get("blocked") or 0),
            "log": find.get("session_path") or "",
        }

    def _find_step(self, result: dict, now: float) -> None:
        find = self.find
        if find is None:
            return
        if find.get("backend") == "place":
            self._find_step_place(result, now)
            return
        unix_ms = int(result.get("capture_unix_ms") or int(self.wall() * 1000))
        if view_blocked(result, self.config):
            find["blocked_frames"] += 1
            self._log_find_frame(find, unix_ms=unix_ms, passed_gate=False, s_item=None, s_bg=None, heat=None)
            find["tracker"].last = Reading(at=now, note="view blocked (finger or cover over the lens)")
            find["guidance"] = (
                "Tracking paused: the camera is covered. Move fingers away from the lens; "
                "ReTrace will recover when the view is clear again. No vibration for a blocked view."
            )
            return
        self._log_find_frame(find, unix_ms=unix_ms, passed_gate=True, s_item=None, s_bg=None, heat=None)
        started = time.perf_counter()
        # Build a lightweight result dict that uses visible keypoints for matching.
        query = {
            "keypoints": result.get("visible_keypoints") or result["keypoints"],
            "descriptors": result.get("visible_descriptors")
            if result.get("visible_descriptors") is not None
            else result["descriptors"],
            "feature_count": result.get("visible_feature_count", result["feature_count"]),
        }
        scores = find["matcher"].scores(query)
        find["match_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        find["frames"] += 1
        reading = read_frame(scores, now, self.config, find["matcher"].neighbours)
        if query["feature_count"] < self.config.min_inliers:
            reading.note = "too little detail in the live view"
        self._find_events(find["tracker"].update(reading))
        tracker = find["tracker"]
        if tracker.status == ARRIVED:
            find["guidance"] = f"Arrived: the saved spot for {find['name']} is recognised."
        elif tracker.status == MATCHED:
            find["guidance"] = (
                f"Progress confirmed at breadcrumb {tracker.confirmed_stage} of {tracker.breadcrumbs}. "
                "Look for the next recorded view shown on the dashboard."
            )
        else:
            find["guidance"] = (
                f"Searching for {find['name']}: no confirmed landmark yet. "
                "Keep looking around; ReTrace rejects ambiguous matches."
            )

    def _find_step_place(self, result: dict, now: float) -> None:
        find = self.find
        finder: PlaceFinder = find["place_finder"]
        from ring.vision import find_frame_ok

        usable = find_frame_ok(result, result.get("imu"), self.config)
        jpeg = result.get("jpeg") or b""
        unix_ms = int(result.get("capture_unix_ms") or int(self.wall() * 1000))
        public = finder.update(jpeg=jpeg, usable=usable, unix_ms=unix_ms, now=now)
        find["public"] = public
        find["frames"] = public.get("frames_compared", find["frames"])
        find["match_ms"] = public.get("match_ms", 0.0)
        find["guidance"] = public.get("guidance", "")
        scored = bool(public.get("scored"))
        state = finder.state
        self._log_find_frame(
            find,
            unix_ms=unix_ms,
            passed_gate=bool(usable),
            s_item=None if not scored or state is None else round(float(state.s_item), 4),
            s_bg=None if not scored or state is None else round(float(state.s_bg), 4),
            heat=None if not scored or state is None else round(float(state.heat), 3),
        )
        if public.get("silent"):
            find["blocked_frames"] = find.get("blocked_frames", 0) + 1
            return
        # Heat-guided pulses (silence = unknown, never cold buzz).
        # The governor, while Find is active, will not apply the per-minute cap and
        # will not send a second pulse inside find_map_min_pulse_interval.
        if public.get("status") == ARRIVED:
            if not find.get("arrival_pulsed"):
                outcome = self.motor.request("arrival", f"{find['name']}: place arrived")
                if not outcome.get("coalesced"):
                    find["feedback"].append(outcome)
                self._note_find_request(find, outcome)
                find["arrival_pulsed"] = True
                self.notice("find", f"Arrived at {find['name']}." + self._pulse_text(outcome))
            return
        interval = float(public.get("pulse_interval_s") or finder.pulse_interval(public.get("best_offset_s") or 0))
        # Never ask the governor faster than the firmware tick. The heat curve may wait longer.
        interval = max(interval, self.motor.find_min_interval_s)
        last = finder.state.last_pulse_at if finder.state else None
        if last is not None and now - last < interval:
            self._note_find_request(find, {"sent": False, "blocked": "", "coalesced": True})
            return
        outcome = self.motor.request("guide", f"{find['name']} heat {public.get('heat', 0):.2f}")
        if not outcome.get("coalesced"):
            find["feedback"].append(outcome)
        self._note_find_request(find, outcome)
        if finder.state is not None:
            finder.state.last_pulse_at = now
        if outcome.get("sent"):
            _pdr_click(float(public.get("heat") or 0.0))

    def _find_events(self, events: list[dict]) -> None:
        find = self.find
        if find is None:
            return
        total = find["tracker"].breadcrumbs
        for event in events:
            kind, stage = event["kind"], event.get("stage")
            if kind == "joined":
                self.notice("find", f"On the trail for {find['name']}: confirmed at breadcrumb {stage} of {total}.")
            elif kind == "recovered":
                self.notice("find", f"Match recovered at breadcrumb {stage} of {total}.")
            elif kind == "lost":
                self.notice("find", f"Match lost. Last confirmed at breadcrumb {stage} of {total}.")
            elif kind == "progress":
                outcome = self.motor.request("progress", f"{find['name']}: breadcrumb {stage} of {total}")
                if not outcome.get("coalesced"):
                    find["feedback"].append(outcome)
                self._note_find_request(find, outcome)
                self.notice("find", f"Confirmed closer: breadcrumb {stage} of {total}." + self._pulse_text(outcome))
            elif kind == "arrived":
                outcome = self.motor.request("arrival", f"{find['name']}: saved spot recognised")
                if not outcome.get("coalesced"):
                    find["feedback"].append(outcome)
                self._note_find_request(find, outcome)
                find["ended"] = "arrived"
                self.store.set_arrived(find["item_id"], int(self.wall() * 1000))
                self._refresh_items()
                self.notice("arrived", f"Saved spot for {find['name']} recognised." + self._pulse_text(outcome))

    def _pulse_text(self, outcome: dict) -> str:
        if outcome["sent"]:
            return " One vibration command sent."
        if outcome.get("coalesced"):
            return " Already covered by a pulse in this interval."
        return f" No vibration: {outcome['blocked']}."

    # ---- motor -----------------------------------------------------------------------

    def set_motor_enabled(self, enabled: bool) -> dict:
        self.motor.enabled = bool(enabled)
        self.notice("motor", "Vibration feedback switched " + ("on." if enabled else "off."))
        return {"enabled": self.motor.enabled}

    def ingest_sensor_logger(self, message: dict) -> dict:
        now = self.clock()
        self.body.ingest(message if isinstance(message, dict) else {}, now)
        self.publish()
        return self.body.public(self.clock())

    def body_recalibrate(self) -> dict:
        report = self.body.recalibrate_offset()
        self.publish()
        self.notice("saved", "Pocket offset cleared. Walk a few steps and it will freeze again.")
        return report

    def body_arm_calibration(self) -> dict:
        armed = self.body.arm_calibration()
        self.publish()
        return armed

    def body_calibrate(self, metres: float) -> dict:
        try:
            length = self.body.calibrate(float(metres))
        except ValueError as error:
            raise UserError(str(error), "bad_step") from None
        self.config.body_step_length = length
        self._write_body_step_length(length)
        self.publish()
        self.notice("saved", f"Phone step length set to {length:.2f} m.")
        return {"step_length": round(length, 3)}

    def body_loop_start(self) -> dict:
        started = self.body.loop_start()
        self.publish()
        return started

    def body_loop_end(self) -> dict:
        try:
            report = self.body.loop_end()
        except ValueError as error:
            raise UserError(str(error), "no_loop") from None
        self.publish()
        self.notice(
            "find",
            f"Loop closed: {report['steps']} steps, path {report['path_m']:.1f} m, "
            f"closure error {report['closure_m']:.1f} m.",
        )
        return report

    def _write_body_step_length(self, length: float) -> None:
        path = getattr(self, "config_path", None)
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            if not isinstance(data, dict):
                return
            data["body_step_length"] = round(float(length), 4)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            self.notice("error", "The step length is in use, but ring_config.json could not be updated.")

    def _body_target(self) -> tuple[float, float, str] | None:
        find = self.find
        if find is None:
            return None
        item = next((row for row in self._items if row["id"] == find["item_id"]), None)
        if item is None or item.get("pdr_x") is None or item.get("pdr_y") is None:
            return None
        return float(item["pdr_x"]), float(item["pdr_y"]), str(item.get("name") or find.get("name") or "")

    def _aim_body(self, now: float) -> None:
        target = self._body_target()
        if target is None:
            self.body.guide = None
            return
        tx, ty, name = target
        guide = self.body.guide_to(tx, ty, name)
        self.body.guide = guide
        self._note_body_track(now, guide)
        if guide is None or guide.get("interval_s") is None:
            return
        if self.body.last_packet is None or now - self.body.last_packet > 3.0:
            return
        interval = float(guide["interval_s"])
        if self._body_pulse_at is not None and now - self._body_pulse_at < interval:
            return
        previous_interval = self.motor.find_min_interval_s
        previous_finding = self.motor.finding
        self.motor.finding = True
        self.motor.find_min_interval_s = max(0.28, interval)
        detail = f"{name} {guide['distance_m']:.1f} m, bearing {guide['error_deg']:+.0f}°"
        outcome = self.motor.request("guide", detail)
        self.motor.find_min_interval_s = previous_interval
        self.motor.finding = previous_finding
        if self.find is not None:
            self.find.setdefault("feedback", []).append(outcome)
            self._note_find_request(self.find, outcome)
        if outcome.get("sent"):
            self._body_pulse_at = now

    def test_pulse(self) -> dict:
        outcome = self.motor.request("manual test", "Test button")
        self.notice("motor", "Test pulse." + self._pulse_text(outcome))
        return {"sent": outcome["sent"], "blocked": outcome["blocked"]}

    # ---- housekeeping ----------------------------------------------------------------

    def tick(self) -> None:
        now = self.clock()
        for event in self.link.drain_events():
            if event["kind"] == "connected":
                self.notice("link", f"Ring connected on {event['detail']}.")
            elif event["kind"] == "disconnected":
                self.notice(
                    "link",
                    f"Ring disconnected ({event['detail']}). Saved items are kept. Waiting for it to come back.",
                )
                self.live_view_keyframe = None
                self.last_frame = {
                    "kept": False,
                    "observed": False,
                    "landmark": False,
                    "keyframe_id": None,
                    "verdict": "disconnected",
                    "text": "no camera",
                }
            else:
                self.notice("link", f"{event['kind']}: {event['detail']}")
        if self.capture is not None and now >= self.capture["until"]:
            self._capture_finish()
        for item_id, trail in list(self.trails.items()):
            if now - trail["started"] > self.config.trail_max_minutes * 60:
                self._close_trail(item_id, f"time limit of {self.config.trail_max_minutes:g} minutes reached")
        if self.find is not None and self.find.get("tracker") is not None:
            self._find_events(self.find["tracker"].tick(now))
            if not self.find["ended"] and now - self.find["started"] > self.config.find_max_minutes * 60:
                self.notice(
                    "find",
                    f"Stopped finding {self.find['name']} after {self.config.find_max_minutes:g} minutes.",
                )
                self.find["ended"] = "timeout"
                self.stop_find()
        self._flush_pending_vlm()
        if now - self._prune_at >= 10.0:
            self._prune_at = now
            removed = self.store.prune(
                self.config.memory_max_frames,
                self.config.memory_max_minutes,
                self.config.memory_max_mb,
                int(self.wall() * 1000),
                self.config.landmark_max_frames,
                self.config.keyframe_bank_max,
            )
            if removed:
                self._usage_at = -1e9
        self._pdr_settle()
        self._note_imu_drops()

    # ---- what the dashboard sees -----------------------------------------------------

    def _body_pin_current(self, item: dict) -> bool:
        """A pin belongs to this process. A quiet phone does not start a new frame."""
        return (
            item.get("pdr_x") is not None
            and item.get("pdr_y") is not None
            and bool(item.get("body_frame"))
            and item.get("body_frame") == self.body.frame_id
        )

    def _start_find_body(self, item_id: int, item: dict) -> dict:
        """Find from the phone's walked position when the camera has no anchor."""
        if not self._body_pin_current(item):
            raise UserError(
                "That pin is from an earlier session. Stand at the object and press Save again.",
                "stale_frame",
            )
        now = self.clock()
        self.find = {
            "backend": "body",
            "item_id": item_id,
            "name": item["name"],
            "started": now,
            "ended": "",
            "feedback": [],
            "guidance": f"Walking to {item['name']} from the phone in your pocket.",
            "public": {},
            "target_x": float(item["pdr_x"]),
            "target_y": float(item["pdr_y"]),
        }
        self.notice("find", self.find["guidance"])
        self._open_find_session(self.find)
        return {"item_id": item_id, "name": item["name"], "backend": "body"}

    def _find_public(self, now: float) -> dict | None:
        find = self.find
        if find is None:
            return None
        if find.get("backend") == "body":
            guide = self.body.guide or {}
            distance = guide.get("distance_m")
            error = guide.get("error_deg")
            if distance is None:
                guidance = f"Finding {find['name']}. Waiting for a phone heading."
                status = "SEARCHING"
            elif guide.get("arrived"):
                guidance = f"{find['name']} is under 1 m. Arrival burst."
                status = "ARRIVED"
            elif error is not None and abs(error) <= 25:
                turn = "straight" if abs(error) < 1 else ("right" if error > 0 else "left")
                guidance = (
                    f"{distance:.1f} m to {find['name']}, bearing {guide.get('bearing', 0):.0f}°. "
                    f"Error {error:+.0f}° ({turn})."
                )
                status = "SEARCHING"
            else:
                guidance = (
                    f"{distance:.1f} m to {find['name']}, bearing {guide.get('bearing', 0):.0f}°. "
                    f"Error {error:+.0f}°. Pulse every 2 s until you are within 25°."
                )
                status = "SEARCHING"
            find["guidance"] = guidance
            return {
                "backend": "body",
                "item_id": find["item_id"],
                "name": find["name"],
                "status": status,
                "phase": status,
                "ended": find.get("ended", ""),
                "guidance": guidance,
                "distance_m": distance,
                "pulse_interval_s": guide.get("interval_s"),
                "heat": 0.0,
                "silent": False,
                "inliers": 0,
                "breadcrumbs": 0,
                "references": [],
                "frames_compared": 0,
                "frames_blocked": 0,
                "match_ms": 0.0,
                "seconds": round(now - find["started"], 1),
                "reading": {"note": guidance, "inliers": 0},
                "session": self._session_public(find),
                "feedback": [
                    {"reason": entry["reason"], "sent": entry["sent"], "blocked": entry["blocked"]}
                    for entry in find.get("feedback", [])[-6:]
                ],
            }
        if find.get("backend") == "pdr":
            place = find.get("public") or {}
            return {
                "backend": "pdr",
                "item_id": find["item_id"],
                "name": find["name"],
                "place": place.get("place") or find.get("place") or "",
                "status": place.get("status", "SEARCHING"),
                "phase": place.get("phase", "SEARCHING"),
                "ended": find.get("ended", ""),
                "anchor_quality": find.get("anchor_quality"),
                "heat": place.get("heat", 0.0),
                "distance_m": place.get("distance_m"),
                "last_seen_keyframe": place.get("last_seen_keyframe"),
                "matched_keyframe": place.get("matched_keyframe"),
                "pulse_interval_s": place.get("pulse_interval_s"),
                "silent": False,
                "inliers": 0,
                "breadcrumbs": 0,
                "confirmed_stage": None,
                "closest_stage": None,
                "confirmed_age_s": None,
                "next_keyframe": None,
                "visited": [],
                "stages": [],
                "references": find.get("references") or [],
                "seconds": round(now - find["started"], 1),
                "frames_compared": find.get("frames", 0),
                "frames_blocked": 0,
                "match_ms": 0.0,
                "guidance": place.get("guidance") or find.get("guidance", ""),
                "reading": {
                    "note": place.get("guidance") or "",
                    "stage": None,
                    "inliers": 0,
                    "reference_inliers": 0,
                    "reference_hit": False,
                    "ambiguous": False,
                    "age_s": 0.0,
                },
                "session": self._session_public(find),
                "feedback": [
                    {"reason": entry["reason"], "sent": entry["sent"], "blocked": entry["blocked"]}
                    for entry in find["feedback"][-6:]
                ],
            }
        if find.get("backend") == "place":
            place = find.get("public") or {}
            if find.get("place_finder") is not None:
                place = find["place_finder"].public(now)
                find["public"] = place
            return {
                "backend": "place",
                "item_id": find["item_id"],
                "name": find["name"],
                "place": place.get("place") or find.get("place") or "",
                "status": place.get("status", UNKNOWN),
                "phase": place.get("phase", "SEARCHING"),
                "ended": find["ended"],
                "anchor_quality": find.get("anchor_quality"),
                "heat": place.get("heat", 0.0),
                "heat_raw": place.get("heat_raw", 0.0),
                "s_item": place.get("s_item"),
                "s_bg": place.get("s_bg"),
                "best_anchor_keyframe": place.get("best_anchor_keyframe"),
                "last_seen_keyframe": place.get("last_seen_keyframe"),
                "matched_keyframe": place.get("best_anchor_keyframe"),
                "next_keyframe": None,
                "inliers": place.get("inliers", 0),
                "vram_mb": place.get("vram_mb"),
                "silent": place.get("silent", False),
                "pulse_interval_s": place.get("pulse_interval_s"),
                "anchor_count": place.get("anchor_count", 0),
                "breadcrumbs": 0,
                "confirmed_stage": None,
                "closest_stage": None,
                "confirmed_age_s": None,
                "visited": [],
                "stages": find.get("stages") or [],
                "references": find.get("references") or [],
                "seconds": place.get("seconds", round(now - find["started"], 1)),
                "frames_compared": place.get("frames_compared", find["frames"]),
                "frames_blocked": find.get("blocked_frames", 0),
                "match_ms": place.get("match_ms", find["match_ms"]),
                "guidance": place.get("guidance") or find.get("guidance", ""),
                "reading": {
                    "note": place.get("guidance") or "",
                    "stage": None,
                    "inliers": place.get("inliers", 0),
                    "reference_inliers": 0,
                    "reference_hit": False,
                    "ambiguous": False,
                    "age_s": 0.0,
                },
                "session": self._session_public(find),
                "feedback": [
                    {"reason": entry["reason"], "sent": entry["sent"], "blocked": entry["blocked"]}
                    for entry in find["feedback"][-6:]
                ],
            }
        tracker: TrailTracker = find["tracker"]
        reading: Reading | None = tracker.last
        stage = tracker.confirmed_stage
        stages = find["stages"]
        next_keyframe = None
        if tracker.status == MATCHED and stage is not None and stage > 0:
            # stages[0] is the saved spot; stages[k] is breadcrumb k.
            index = stage - 1
            if 0 <= index < len(stages):
                next_keyframe = stages[index]
        phase = "SEARCHING"
        if tracker.status == ARRIVED:
            phase = "ARRIVED"
        elif tracker.status == MATCHED:
            phase = "PROGRESS CONFIRMED"
        elif reading is not None and "blocked" in (reading.note or ""):
            phase = "TRACKING LOST"
        elif tracker.confirmed_stage is not None and tracker.status == UNKNOWN:
            phase = "TRACKING LOST"
        elif reading is not None and reading.stage is not None and not reading.ambiguous:
            phase = "LANDMARK RECOGNIZED"
        return {
            "backend": "orb",
            "item_id": find["item_id"],
            "name": find["name"],
            "status": tracker.status,
            "phase": phase,
            "ended": find["ended"],
            "anchor_quality": find.get("anchor_quality"),
            "breadcrumbs": tracker.breadcrumbs,
            "confirmed_stage": stage,
            "closest_stage": tracker.closest_stage,
            "confirmed_age_s": None if tracker.last_confirmed_at is None else round(now - tracker.last_confirmed_at, 1),
            "matched_keyframe": tracker.confirmed_keyframe if tracker.status != UNKNOWN else None,
            "next_keyframe": next_keyframe,
            "visited": sorted(tracker.visited),
            "stages": stages,
            "references": find["references"],
            "seconds": round(now - find["started"], 1),
            "frames_compared": find["frames"],
            "frames_blocked": find["blocked_frames"],
            "match_ms": find["match_ms"],
            "guidance": find.get("guidance", ""),
            "reading": None
            if reading is None
            else {
                "note": reading.note,
                "stage": reading.stage,
                "inliers": reading.inliers,
                "reference_inliers": reading.ref_inliers,
                "reference_hit": reading.ref_hit,
                "ambiguous": reading.ambiguous,
                "age_s": round(now - reading.at, 1),
            },
            "session": self._session_public(find),
            "feedback": [
                {"reason": entry["reason"], "sent": entry["sent"], "blocked": entry["blocked"]}
                for entry in find["feedback"][-6:]
            ],
        }

    def _mode(self) -> dict:
        """The product-level states the dashboard should emphasise."""
        find = self.find
        capture = self.capture
        save = self.save_state
        if find is not None:
            public = self._find_public(self.clock()) or {}
            return {
                "memory": "CONTINUOUS MEMORY ACTIVE",
                "save": "ITEM SAVED" if self._items else "",
                "anchor": ANCHOR_LABEL.get(find.get("anchor_quality"), ""),
                "search": public.get("phase", "SEARCHING"),
                "find_backend": public.get("backend") or find.get("backend") or self.config.find_backend,
            }
        if capture is not None:
            quality = self.store.compute_anchor_quality(capture["item_id"]) if capture["item_id"] else ANCHOR_MISSING
            return {
                "memory": "CONTINUOUS MEMORY ACTIVE",
                "save": "ITEM SAVED",
                "anchor": ANCHOR_LABEL.get(quality, ""),
                "search": "IDLE",
            }
        if save and save.get("state") == SAVE_SAVED:
            return {
                "memory": "CONTINUOUS MEMORY ACTIVE",
                "save": "ITEM SAVED",
                "anchor": ANCHOR_LABEL.get(save.get("anchor_quality"), ""),
                "search": "IDLE",
            }
        return {
            "memory": "CONTINUOUS MEMORY ACTIVE",
            "save": "ITEM SAVED" if self._items else "NO ITEM YET",
            "anchor": "",
            "search": "IDLE",
        }

    def publish(self) -> dict:
        now = self.clock()
        self._aim_body(now)
        if now - self._usage_at >= 2.0:
            self._usage = self.store.usage()
            self._usage["recent"] = [
                {
                    "id": frame["id"],
                    "unix_ms": frame["unix_ms"],
                    "features": frame["features"],
                    "role": frame.get("role"),
                    "occlusion": frame.get("occlusion", 0),
                }
                for frame in self.store.recent_keyframes(14)
            ]
            self._usage["recent_landmarks"] = [
                {"id": frame["id"], "unix_ms": frame["unix_ms"], "features": frame["features"]}
                for frame in self.store.recent_keyframes(8, ROLE_LANDMARK)
            ]
            self._usage_at = now
        link = self.link.status()
        config = self.config
        motor = self.motor.public(self.wall() - now)
        motor.update(
            {
                "pulse_ms": FIRMWARE_PULSE_MS,
                "bytes_written": link["writes"],
                "writes_allowed": link["writes_allowed"],
                "board_pulse_starts": link["board_pulse_starts"],
                "board_pulse_ends": link["board_pulse_ends"],
                "board_ignored": link["board_ignored"],
                "board_total_pulses": link["board"].get("pulses"),
                "board_motor": link["board"].get("motor"),
                "last_pulse_on_us": link["last_pulse_on_us"],
            }
        )
        if self.pdr_run is not None:
            self._note_imu_drops()
        shown = self.pdr_run["tracker"] if self.pdr_run is not None else self.pdr
        snapshot = {
            "unix_ms": int(self.wall() * 1000),
            "uptime_s": round(now - self.started_at, 1),
            "mode": self._mode(),
            "link": link,
            "frames": {
                "processed": self.processed,
                "kept": self.landmarks,
                "observed": self.observed,
                "landmarks": self.landmarks,
                "usable": self.usable_count,
                "bank_kept": self.bank_kept,
                "rejected": dict(self.rejected),
                "usable_rejects": dict(self.usable_rejects),
                "last": dict(self.last_frame),
                "process_fps_limit": config.process_fps,
            },
            "find_backend": config.find_backend,
            "pdr": None if shown is None else shown.live(),
            "body": self.body.public(now),
            "pdr_test": self._pdr_public(),
            "pdr_items": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "x": item.get("pdr_x"),
                    "y": item.get("pdr_y"),
                }
                for item in self._items
                if item.get("pdr_x") is not None and item.get("body_frame") == self.body.frame_id
            ],
            "embedder": self.embedder.status(),
            "putdown": {
                "state": self.putdown.state,
                "events": list(self.putdown_events_log)[:8],
            },
            "hold": {
                "state": self.hold.state,
                "events": list(self.hold_events_log)[:8],
            },
            "vlm": self.vlm.status(),
            "memory": {
                **self._usage,
                "limits": {
                    "frames": config.memory_max_frames,
                    "landmarks": config.landmark_max_frames,
                    "bank": config.keyframe_bank_max,
                    "minutes": config.memory_max_minutes,
                    "mb": config.memory_max_mb,
                    "imu_log_mb": config.imu_log_max_mb,
                },
            },
            "items": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "created_unix_ms": item["created_unix_ms"],
                    "references": len(item["references"]),
                    "context": len(item.get("context") or []),
                    "breadcrumbs": len(item["breadcrumbs"]),
                    "cover_keyframe": (item["references"] or item.get("context") or item["breadcrumbs"] or [None])[-1]
                    if (item["references"] or item.get("context") or item["breadcrumbs"])
                    else None,
                    "trail_state": item["trail_state"],
                    "trail_note": item["trail_note"],
                    "x": item.get("pdr_x"),
                    "y": item.get("pdr_y"),
                    "body_frame": item.get("body_frame"),
                    "anchor_quality": item.get("anchor_quality", ANCHOR_MISSING),
                    "anchor_label": ANCHOR_LABEL.get(item.get("anchor_quality", ANCHOR_MISSING), ""),
                    # Place-find only needs a put-down window / frames; ORB-ready anchors are optional.
                    "locatable": (
                        item.get("anchor_quality") == ANCHOR_READY
                        or len(item["breadcrumbs"]) > 0
                        or len(item.get("references") or []) > 0
                        or len(item.get("context") or []) > 0
                        or self.config.find_backend in ("place", "pdr")
                        or item.get("pdr_x") is not None
                    ),
                    "last_arrived_unix_ms": item["last_arrived_unix_ms"],
                }
                for item in self._items
            ],
            "item_limits": {
                "items": config.max_items,
                "references": config.max_reference_frames,
                "context": config.max_context_frames,
                "breadcrumbs": config.trail_max_breadcrumbs,
                "trail_minutes": config.trail_max_minutes,
            },
            "capture": None
            if self.capture is None
            else {
                "item_id": self.capture["item_id"],
                "name": self.capture["name"],
                "references": self.capture["references"],
                "context": self.capture.get("context", 0),
                "state": SAVE_CAPTURING,
                "seconds_left": round(max(0.0, self.capture["until"] - now), 1),
            },
            "save": self._save_public(now),
            "find": self._find_public(now),
            "motor": motor,
            "mapper": self.mapper.status(),
            "notices": list(self.notices),
        }
        self._snapshot = snapshot
        return snapshot

    def snapshot(self) -> dict:
        return self._snapshot
