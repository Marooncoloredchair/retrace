"""Match live frames to saved places with ORB descriptors.

A place is named only from geometric inlier counts. Those counts are not a
distance and not a probability. Reference frames are never scored as tests.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("retrace.places")

PLACES = ("A", "B", "C")
TRUTHS = ("A", "B", "C", "UNFAMILIAR", "HAND")
CAMERAS = ("palm", "outward")
MIN_INLIERS = 12
INLIER_MARGIN = 8
LOWE_RATIO = 0.75
RANSAC_REPROJ_PX = 3.0
MAX_REFERENCES_PER_PLACE = 40
MAX_TEST_ROWS = 100_000
STABLE_WINDOW = 10
STABLE_REQUIRED = 3
AWAY = "AWAY"
ARRIVAL_WINDOW_MS = 10_000
MARK_EVENTS = {
    "LEFT_SAVED_LOCATION": AWAY,
    "ARRIVED_A": "A",
    "ARRIVED_B": "B",
    "ARRIVED_C": "C",
}
FAILURE_ZERO = "zero_features"
FAILURE_MATCHES = "too_few_matches"
FAILURE_GEOMETRY = "failed_geometry"
FAILURE_INLIERS = "too_few_inliers"
FAILURE_MARGIN = "margin"
FAILURE_NAMED = "named"
FAILURE_NONE = "no_references"
FAILURE_TEXT = {
    FAILURE_ZERO: "Zero features. ORB found nothing on this frame, so there is nothing to match.",
    FAILURE_MATCHES: "Too few descriptor matches. Features are present, and fewer than 4 survived the Lowe ratio against the closest reference.",
    FAILURE_GEOMETRY: "Geometric check failed. Descriptor matches exist, and they do not agree on one homography.",
    FAILURE_INLIERS: "Too few inliers. A homography was found, and it has fewer than 12 inliers.",
    FAILURE_MARGIN: "Another saved place is within 8 inliers, so this frame stays UNKNOWN.",
    FAILURE_NAMED: "Named from the inlier counts.",
    FAILURE_NONE: "No saved references are loaded.",
}
FAILURE_SHORT = {
    FAILURE_ZERO: "Zero features",
    FAILURE_MATCHES: "Too few descriptor matches",
    FAILURE_GEOMETRY: "Geometric check failed",
    FAILURE_INLIERS: "Too few inliers",
    FAILURE_MARGIN: "Places too close",
    FAILURE_NAMED: "Named",
    FAILURE_NONE: "No references",
}
INTERVAL_LABELS = ("same_spot", "arm_only", "returned")
MAX_UNKNOWN_SHOTS = 8
UNKNOWN_SHOT_GAP_MS = 2000

CSV_FIELDS = (
    "timestamp_iso",
    "unix_ms",
    "capture_unix_ms",
    "clock_delta_ms",
    "row_type",
    "camera",
    "ground_truth",
    "reference_place",
    "predicted",
    "outcome",
    "matches_A",
    "inliers_A",
    "matches_B",
    "inliers_B",
    "matches_C",
    "inliers_C",
    "best_place",
    "best_matches",
    "best_inliers",
    "second_place",
    "second_inliers",
    "feature_count",
    "masked",
    "mask_x",
    "mask_y",
    "mask_w",
    "mask_h",
    "match_ms",
    "phone_sequence",
    "observation_id",
    "image_file",
    "raw_predicted",
    "stable_predicted",
    "stable_support",
    "stable_window_A",
    "stable_window_B",
    "stable_window_C",
    "stable_outcome",
    "event",
    "segment",
    "failure",
    "best_reference_id",
    "blur_score",
    "interval",
    "diagnostic_file",
)

PLACE_COLOR = {
    "A": (220, 220, 80),
    "B": (80, 170, 255),
    "C": (230, 120, 220),
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(unix_ms: int) -> str:
    return datetime.fromtimestamp(unix_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _empty_counts() -> dict[str, int]:
    return {place: 0 for place in PLACES}


def outcome_for(ground_truth: str, predicted: str) -> str:
    """Score one held-out frame.

    UNKNOWN is its own result. For Unfamiliar, Hand, and Away, UNKNOWN means
    the frame was not given a saved place. That distinction is counted in the
    confusion matrix; it is not turned into a probability. Frames logged
    before any arrival mark stay unlabeled.
    """
    if ground_truth not in (*PLACES, AWAY, "UNFAMILIAR", "HAND"):
        return "unlabeled"
    if ground_truth in PLACES and predicted == ground_truth:
        return "correct"
    if predicted == "UNKNOWN":
        return "unknown"
    return "incorrect"


def outcome_for_stable(ground_truth: str, stable: str) -> str:
    """Score the windowed name. CONFIRMED A matches only ground truth A."""
    if ground_truth in PLACES and stable == f"CONFIRMED {ground_truth}":
        return "correct"
    if ground_truth not in (*PLACES, AWAY, "UNFAMILIAR", "HAND"):
        return "unlabeled"
    if stable == "UNKNOWN":
        return "unknown"
    return "incorrect"


def stable_from_raw(history: list[str]) -> tuple[str, int, dict[str, int]]:
    """Confirm a place from raw names that already passed the inlier rule.

    The last 10 raw names are kept. A place is confirmed only when at least 3
    of them named that place and none of them named a different place.
    """
    window = list(history)[-STABLE_WINDOW:]
    counts = {place: sum(1 for name in window if name == place) for place in PLACES}
    named = [place for place in PLACES if counts[place]]
    if len(named) == 1 and counts[named[0]] >= STABLE_REQUIRED:
        return f"CONFIRMED {named[0]}", counts[named[0]], counts
    return "UNKNOWN", 0, counts


def _unix_ms(row: dict) -> int | None:
    value = row.get("unix_ms")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def arrival_metrics(rows: list[dict], now_ms: int | None = None) -> dict:
    """Measure each arrival mark from the logged frames. Missing delays stay null."""
    if now_ms is None:
        now_ms = _now_ms()
    events = [
        row
        for row in rows
        if row.get("row_type") == "event"
        and row.get("event")
        and not str(row.get("event")).endswith("_REPEAT")
    ]
    frames = [row for row in rows if row.get("row_type") == "test"]
    visits = []
    away_frames = 0
    away_marks = 0
    away_raw_incorrect = 0
    away_stable_incorrect = 0
    for index, event in enumerate(events):
        start = _unix_ms(event)
        if start is None:
            continue
        end = _unix_ms(events[index + 1]) if index + 1 < len(events) else None
        name = event.get("event")
        if name in MARK_EVENTS and name.startswith("ARRIVED_"):
            place = MARK_EVENTS[name]
            segment_frames = _segment_frames(frames, start, end, place)
            bound = end if end is not None else now_ms
            elapsed = max(0, bound - start)
            window_frames = [
                frame
                for frame in segment_frames
                if (_unix_ms(frame) or 0) < start + ARRIVAL_WINDOW_MS
            ]
            stable_label = f"CONFIRMED {place}"
            raw_hit = next((frame for frame in segment_frames if frame.get("predicted") == place), None)
            stable_hit = next(
                (frame for frame in segment_frames if frame.get("stable_predicted") == stable_label),
                None,
            )
            count = len(window_frames)
            raw_correct = sum(1 for frame in window_frames if frame.get("predicted") == place)
            stable_correct = sum(1 for frame in window_frames if frame.get("stable_predicted") == stable_label)
            dropouts = 0
            previous = None
            for frame in segment_frames:
                current = frame.get("stable_predicted") or "UNKNOWN"
                if previous == stable_label and current != stable_label:
                    dropouts += 1
                previous = current
            last_stamp = _unix_ms(segment_frames[-1]) if segment_frames else start
            segment_ms = max(0, (end if end is not None else last_stamp) - start)
            visits.append(
                {
                    "place": place,
                    "arrived_unix_ms": start,
                    "raw_delay_ms": None if raw_hit is None else (_unix_ms(raw_hit) or start) - start,
                    "stable_delay_ms": None if stable_hit is None else (_unix_ms(stable_hit) or start) - start,
                    "window_frames": count,
                    "window_ms": min(ARRIVAL_WINDOW_MS, elapsed),
                    "window_complete": elapsed >= ARRIVAL_WINDOW_MS,
                    "raw_correct": raw_correct,
                    "stable_correct": stable_correct,
                    "raw_percent": None if count == 0 else round(100.0 * raw_correct / count, 1),
                    "stable_percent": None if count == 0 else round(100.0 * stable_correct / count, 1),
                    "stable_dropouts": dropouts,
                    "segment_frames": len(segment_frames),
                    "segment_ms": segment_ms,
                }
            )
        elif name == "LEFT_SAVED_LOCATION":
            away_marks += 1
            for frame in _segment_frames(frames, start, end, AWAY):
                away_frames += 1
                if frame.get("predicted") in PLACES:
                    away_raw_incorrect += 1
                if str(frame.get("stable_predicted") or "").startswith("CONFIRMED"):
                    away_stable_incorrect += 1
    return {
        "visits": visits,
        "away_frames": away_frames,
        "away_marks": away_marks,
        "away_raw_incorrect": away_raw_incorrect,
        "away_stable_incorrect": away_stable_incorrect,
    }


def _segment_frames(frames: list[dict], start: int, end: int | None, truth: str) -> list[dict]:
    chosen = []
    for frame in frames:
        stamp = _unix_ms(frame)
        if stamp is None or stamp < start:
            continue
        if end is not None and stamp >= end:
            continue
        if frame.get("ground_truth") != truth:
            continue
        chosen.append(frame)
    return chosen


def _mask_rect(mask: dict | None, width: int, height: int) -> tuple[float, float, float, float] | None:
    if not mask or not mask.get("enabled"):
        return None
    x = float(mask["x"]) * width
    y = float(mask["y"]) * height
    w = float(mask["w"]) * width
    h = float(mask["h"]) * height
    if w < 2 or h < 2:
        return None
    return x, y, w, h


def _inside(px: float, py: float, rect: tuple[float, float, float, float]) -> bool:
    x, y, w, h = rect
    return x <= px < x + w and y <= py < y + h


def _filter_mask(keypoints, descriptors, rect):
    if rect is None or descriptors is None or len(keypoints) == 0:
        return keypoints, descriptors
    keep = [index for index, point in enumerate(keypoints) if not _inside(point.pt[0], point.pt[1], rect)]
    if not keep:
        return [], None
    return [keypoints[index] for index in keep], descriptors[keep]


def _keypoints_to_arrays(keypoints) -> dict[str, np.ndarray]:
    count = len(keypoints)
    pts = np.zeros((count, 2), dtype=np.float32)
    size = np.zeros(count, dtype=np.float32)
    angle = np.zeros(count, dtype=np.float32)
    response = np.zeros(count, dtype=np.float32)
    octave = np.zeros(count, dtype=np.int32)
    for index, point in enumerate(keypoints):
        pts[index, 0] = point.pt[0]
        pts[index, 1] = point.pt[1]
        size[index] = point.size
        angle[index] = point.angle
        response[index] = point.response
        octave[index] = int(point.octave)
    return {"pts": pts, "size": size, "angle": angle, "response": response, "octave": octave}


def _arrays_to_keypoints(data) -> list:
    pts = data["pts"]
    keypoints = []
    for index in range(len(pts)):
        keypoints.append(
            cv2.KeyPoint(
                x=float(pts[index, 0]),
                y=float(pts[index, 1]),
                size=float(data["size"][index]),
                angle=float(data["angle"][index]),
                response=float(data["response"][index]),
                octave=int(data["octave"][index]),
            )
        )
    return keypoints


def _blank_match(stage: str) -> dict:
    return {"matches": 0, "inliers": 0, "stage": stage, "pairs": []}


def _correspondence(query_kp, ref_kp, match, inlier: bool) -> dict:
    query = query_kp[match.queryIdx].pt
    reference = ref_kp[match.trainIdx].pt
    return {
        "qx": float(query[0]),
        "qy": float(query[1]),
        "rx": float(reference[0]),
        "ry": float(reference[1]),
        "inlier": inlier,
    }


def _keep_pairs(pairs: list[dict]) -> list[dict]:
    """Keep every inlier and a short sample of the other descriptor matches."""
    inliers = [item for item in pairs if item["inlier"]]
    others = [item for item in pairs if not item["inlier"]]
    return inliers + others[:40]


def match_descriptors(query_kp, query_desc, ref_kp, ref_desc) -> dict:
    """Compare one live frame with one reference and name the step that stopped it."""
    if query_desc is None or len(query_kp) == 0 or len(query_desc) == 0:
        return _blank_match("empty_query")
    if ref_desc is None or len(ref_kp) < 2 or len(ref_desc) < 2 or len(query_kp) < 2 or len(query_desc) < 2:
        return _blank_match(FAILURE_MATCHES)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    try:
        pairs = matcher.knnMatch(query_desc, ref_desc, k=2)
    except cv2.error:
        return _blank_match(FAILURE_MATCHES)
    good = []
    for item in pairs:
        if len(item) < 2:
            continue
        first, second = item
        if first.distance < LOWE_RATIO * second.distance:
            good.append(first)
    drawn = [_correspondence(query_kp, ref_kp, item, False) for item in good]
    if len(good) < 4:
        return {"matches": len(good), "inliers": 0, "stage": FAILURE_MATCHES, "pairs": _keep_pairs(drawn)}
    src = np.float32([query_kp[item.queryIdx].pt for item in good])
    dst = np.float32([ref_kp[item.trainIdx].pt for item in good])
    try:
        _homography, mask = cv2.findHomography(src, dst, cv2.RANSAC, RANSAC_REPROJ_PX)
    except cv2.error:
        return {"matches": len(good), "inliers": 0, "stage": FAILURE_GEOMETRY, "pairs": _keep_pairs(drawn)}
    if mask is None:
        return {"matches": len(good), "inliers": 0, "stage": FAILURE_GEOMETRY, "pairs": _keep_pairs(drawn)}
    flags = mask.ravel().astype(bool)
    for index, flag in enumerate(flags):
        drawn[index]["inlier"] = bool(flag)
    inlier_count = int(flags.sum())
    stage = "enough_inliers" if inlier_count >= MIN_INLIERS else FAILURE_INLIERS
    return {"matches": len(good), "inliers": inlier_count, "stage": stage, "pairs": _keep_pairs(drawn)}


def _public_stage(stage: str, feature_count: int) -> str:
    if int(feature_count) <= 0:
        return FAILURE_ZERO
    if stage == "empty_query":
        return FAILURE_MATCHES
    return stage


def frame_failure(feature_count: int, best_stage: str, best_inliers: int, second_inliers: int, predicted: str) -> str:
    """Why this frame received its name. The 12-inlier rule is unchanged."""
    if int(feature_count) <= 0:
        return FAILURE_ZERO
    if predicted in PLACES:
        return FAILURE_NAMED
    stage = _public_stage(best_stage, feature_count)
    if stage in {FAILURE_ZERO, FAILURE_MATCHES, FAILURE_GEOMETRY}:
        return stage
    if best_inliers >= MIN_INLIERS and best_inliers < second_inliers + INLIER_MARGIN:
        return FAILURE_MARGIN
    return FAILURE_INLIERS


def recognize(result: dict, references: list[dict], mask: dict | None) -> dict:
    """Compare one live frame with every saved reference."""
    started = time.perf_counter()
    width = int(result["width"])
    height = int(result["height"])
    feature_count = int(result["feature_count"])
    rect = _mask_rect(mask, width, height)
    query_kp, query_desc = _filter_mask(result["keypoints"], result["descriptors"], rect)
    matches = _empty_counts()
    inliers = _empty_counts()
    per_place = {}
    for place in PLACES:
        per_place[place] = {
            "matches": 0,
            "inliers": 0,
            "stage": FAILURE_ZERO if feature_count <= 0 else FAILURE_MATCHES,
            "reference_id": "",
            "has_photo": False,
            "pairs": [],
            "image": None,
            "keypoints": [],
        }
    for reference in references:
        ref_kp, ref_desc = _filter_mask(reference["keypoints"], reference["descriptors"], rect)
        compared = match_descriptors(query_kp, query_desc, ref_kp, ref_desc)
        place = reference["place"]
        current = per_place[place]
        better = (
            not current["reference_id"]
            or compared["inliers"] > current["inliers"]
            or (compared["inliers"] == current["inliers"] and compared["matches"] > current["matches"])
        )
        if not better:
            continue
        current["matches"] = compared["matches"]
        current["inliers"] = compared["inliers"]
        current["stage"] = _public_stage(compared["stage"], feature_count)
        current["reference_id"] = reference.get("id", "")
        current["has_photo"] = reference.get("image") is not None
        current["pairs"] = compared["pairs"]
        current["image"] = reference.get("image")
        current["keypoints"] = ref_kp
        matches[place] = compared["matches"]
        inliers[place] = compared["inliers"]
    saved_places = {item["place"] for item in references}
    for place in PLACES:
        if place not in saved_places:
            per_place[place]["stage"] = FAILURE_NONE
    ranked = sorted(PLACES, key=lambda place: (inliers[place], matches[place]), reverse=True)
    present = [place for place in ranked if any(item["place"] == place for item in references)]
    best_place = present[0] if present else ""
    second_place = present[1] if len(present) > 1 else ""
    best_inliers = inliers[best_place] if best_place else 0
    second_inliers = inliers[second_place] if second_place else 0
    if best_place and best_inliers >= MIN_INLIERS and best_inliers >= second_inliers + INLIER_MARGIN:
        predicted = best_place
    else:
        predicted = "UNKNOWN"
    winner = per_place[best_place] if best_place else None
    best_stage = winner["stage"] if winner else FAILURE_NONE
    failure = FAILURE_NONE if not references else frame_failure(
        feature_count, best_stage, best_inliers, second_inliers, predicted
    )
    by_place = {}
    for place in PLACES:
        item = per_place[place]
        by_place[place] = {
            "matches": item["matches"],
            "inliers": item["inliers"],
            "stage": item["stage"],
            "reference_id": item["reference_id"],
            "has_photo": item["has_photo"],
        }
    elapsed = (time.perf_counter() - started) * 1000.0
    return {
        "predicted": predicted,
        "matches": matches,
        "inliers": inliers,
        "best_place": best_place,
        "best_matches": matches[best_place] if best_place else 0,
        "best_inliers": best_inliers,
        "second_place": second_place,
        "second_inliers": second_inliers,
        "inlier_points": [(item["qx"], item["qy"]) for item in (winner["pairs"] if winner else []) if item["inlier"]],
        "match_ms": round(elapsed, 2),
        "failure": failure,
        "best_reference_id": winner["reference_id"] if winner else "",
        "blur_score": result.get("blur_score", ""),
        "feature_count": feature_count,
        "by_place": by_place,
        "reference_image": None if winner is None else winner["image"],
        "reference_keypoints": [] if winner is None else winner["keypoints"],
        "pairs": [] if winner is None else winner["pairs"],
    }


def _draw_keypoints(canvas, keypoints, rect) -> None:
    for point in keypoints:
        center = (int(round(point.pt[0])), int(round(point.pt[1])))
        if rect is not None and _inside(point.pt[0], point.pt[1], rect):
            color = (140, 140, 140)
        else:
            color = (80, 220, 120)
        cv2.circle(canvas, center, 2, color, 1, lineType=cv2.LINE_AA)


def draw_place_frame(result: dict, decision: dict | None, mask: dict | None) -> bytes | None:
    """Live frame beside the best saved reference, with matches and inliers drawn."""
    image = result["image"]
    if image is None:
        return None
    left = image.copy()
    height, width = left.shape[:2]
    rect = _mask_rect(mask, width, height)
    _draw_keypoints(left, result["keypoints"], rect)
    reference = None if decision is None else decision.get("reference_image")
    if reference is None:
        right = np.full_like(left, 24)
        cv2.putText(
            right,
            "No saved photo",
            (16, height // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (220, 220, 220),
            1,
            cv2.LINE_AA,
        )
        scale_x = 1.0
        scale_y = 1.0
    else:
        ref_height, ref_width = reference.shape[:2]
        scale_x = width / ref_width
        scale_y = height / ref_height
        right = reference.copy()
        if ref_width != width or ref_height != height:
            right = cv2.resize(right, (width, height))
        for point in decision.get("reference_keypoints") or []:
            center = (int(round(point.pt[0] * scale_x)), int(round(point.pt[1] * scale_y)))
            cv2.circle(right, center, 2, (80, 220, 120), 1, lineType=cv2.LINE_AA)
    canvas = np.hstack([left, right])
    color_name = decision["best_place"] if decision and decision.get("best_place") else ""
    inlier_color = PLACE_COLOR.get(color_name, (80, 220, 120))
    pairs = [] if decision is None else decision.get("pairs") or []
    for pair in pairs:
        if pair["inlier"]:
            continue
        start = (int(round(pair["qx"])), int(round(pair["qy"])))
        end = (int(round(pair["rx"] * scale_x)) + width, int(round(pair["ry"] * scale_y)))
        cv2.line(canvas, start, end, (150, 160, 80), 1, cv2.LINE_AA)
    for pair in pairs:
        if not pair["inlier"]:
            continue
        start = (int(round(pair["qx"])), int(round(pair["qy"])))
        end = (int(round(pair["rx"] * scale_x)) + width, int(round(pair["ry"] * scale_y)))
        cv2.circle(canvas, start, 4, inlier_color, 1, lineType=cv2.LINE_AA)
        cv2.circle(canvas, end, 4, inlier_color, 1, lineType=cv2.LINE_AA)
        cv2.line(canvas, start, end, inlier_color, 2, cv2.LINE_AA)
    failure = FAILURE_NONE if decision is None else decision.get("failure") or FAILURE_NONE
    blur = result.get("blur_score", "")
    features = int(result["feature_count"])
    inlier_count = 0 if decision is None else decision.get("best_inliers", 0)
    caption = f"{FAILURE_SHORT.get(failure, failure)}  blur {blur}  features {features}  inliers {inlier_count}"
    cv2.rectangle(canvas, (0, height - 22), (width * 2, height), (0, 0, 0), -1)
    cv2.putText(canvas, "Live", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(canvas, "Best reference", (width + 8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(canvas, caption, (8, height - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1, cv2.LINE_AA)
    ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        return None
    sequence = int(result["sequence"]) & 0xFFFFFFFF
    capture = int(result["capture_unix_ms"]) & 0xFFFFFFFFFFFFFFFF
    return struct.pack(">IQ", sequence, capture) + encoded.tobytes()


class Places:
    """Saved references and a separate held-out test walk."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.feature_dir = root / "features"
        self.image_dir = root / "images"
        self.index_path = root / "index.json"
        self.test_path = root / "tests.csv"
        self.cond = asyncio.Condition()
        self._lock = threading.Lock()
        self.references: list[dict] = []
        self.rows: list[dict] = []
        self.latest: dict | None = None
        self.decision: dict | None = None
        self.clock_delta_ms: int | None = None
        self.testing = False
        self.ground_truth = ""
        self.segment = ""
        self.raw_history: list[str] = []
        self.extra_presses = 0
        self.last_mark: dict | None = None
        self.last_archive = ""
        self.camera = "palm"
        self.save_images = False
        self.diagnostic = False
        self.save_unknown = False
        self.unknown_shots = 0
        self.last_unknown_shot_ms = 0
        self.diagnostic_dir = root / "diagnostics"
        self.audit: dict = {}
        self.missing_features: list[str] = []
        self.guided = None
        self.mask: dict | None = None
        self.packet: bytes | None = None
        self.version = 0
        self.skipped_reference_frames = 0
        self._load()

    def _load(self) -> None:
        if self.index_path.exists():
            try:
                payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("Could not read saved places")
                payload = {}
            for item in payload.get("references", []):
                feature_path = self.feature_dir / f"{item['id']}.npz"
                if not feature_path.exists():
                    self.missing_features.append(
                        f"{item.get('place', '?')} {item.get('id', '')} is in the index and its feature file is missing"
                    )
                    continue
                with np.load(feature_path) as data:
                    descriptors = data["descriptors"]
                    keypoints = _arrays_to_keypoints(data)
                if descriptors.size == 0:
                    descriptors = None
                image = self._read_reference_image(item.get("image_file") or "")
                self.references.append({**item, "keypoints": keypoints, "descriptors": descriptors, "image": image})
        self.audit = self._build_audit()
        if self.test_path.exists():
            try:
                with self.test_path.open(encoding="utf-8", newline="") as handle:
                    self.rows = list(csv.DictReader(handle))
            except OSError:
                log.warning("Could not read saved place tests")
            self.rows = self.rows[-MAX_TEST_ROWS:]

    def _write_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        public = []
        for item in self.references:
            public.append(
                {key: value for key, value in item.items() if key not in {"keypoints", "descriptors", "image", "pairs"}}
            )
        temporary = self.index_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({"references": public}, indent=2), encoding="utf-8")
        temporary.replace(self.index_path)

    def _append_test_row(self, row: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.test_path.exists():
            with self.test_path.open(encoding="utf-8", newline="") as handle:
                header = handle.readline().strip()
            if header.split(",") != list(CSV_FIELDS):
                self._rewrite_tests()
                return
        new_file = not self.test_path.exists()
        with self.test_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _write_rows(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
        temporary.replace(path)

    def _rewrite_tests(self) -> None:
        self._write_rows(self.test_path, self.rows)

    def _archive_log(self) -> str:
        """Move the current test log aside so the next walk starts empty."""
        if not self.rows:
            return ""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = self.root / f"tests-{stamp}.csv"
        self._write_rows(archive, self.rows)
        if self.test_path.exists():
            self.test_path.unlink()
        self.rows = []
        self.skipped_reference_frames = 0
        return archive.name

    def _reference_identity(self, sequence: int, capture_unix_ms: int) -> tuple[int, int]:
        return int(sequence), int(capture_unix_ms)

    def _identities(self) -> set[tuple[int, int]]:
        return {self._reference_identity(item["phone_sequence"], item["capture_unix_ms"]) for item in self.references}

    async def observe(self, result: dict, now: float) -> None:
        with self._lock:
            references = [
                {
                    "id": item["id"],
                    "place": item["place"],
                    "keypoints": item["keypoints"],
                    "descriptors": item["descriptors"],
                    "image": item.get("image"),
                }
                for item in self.references
            ]
            mask = dict(self.mask) if self.mask else None
            camera = self.camera
            save_images = self.save_images
            identities = self._identities()
        decision = None
        if references:
            decision = await asyncio.to_thread(recognize, result, references, mask)
        packet = await asyncio.to_thread(draw_place_frame, result, decision, mask)
        if decision is not None:
            decision.pop("reference_image", None)
            decision.pop("reference_keypoints", None)
            decision.pop("pairs", None)
        unix_ms = int(now * 1000)
        capture = int(result["capture_unix_ms"])
        identity = self._reference_identity(result["sequence"], capture)
        async with self.cond:
            self.latest = result
            if decision is not None:
                self.raw_history.append(decision["predicted"])
                if len(self.raw_history) > STABLE_WINDOW:
                    self.raw_history = self.raw_history[-STABLE_WINDOW:]
                stable, support, counts = stable_from_raw(self.raw_history)
                decision["stable_predicted"] = stable
                decision["stable_support"] = support
                decision["stable_counts"] = counts
            self.decision = decision
            self.clock_delta_ms = unix_ms - capture
            self.packet = packet
            self.version += 1
            testing = self.testing
            segment = self.segment
            logged_truth = segment if segment in (*PLACES, AWAY) else "UNLABELED"
            if testing and decision is not None and identity not in identities:
                row = self._test_row(
                    unix_ms,
                    result,
                    decision,
                    logged_truth,
                    self.camera or camera,
                    mask,
                    self.save_images if testing else save_images,
                    segment,
                )
                if (
                    self.save_unknown
                    and decision.get("predicted") == "UNKNOWN"
                    and self.unknown_shots < MAX_UNKNOWN_SHOTS
                    and unix_ms - self.last_unknown_shot_ms >= UNKNOWN_SHOT_GAP_MS
                    and packet
                ):
                    name = f"unknown-{row['observation_id']}.jpg"
                    row["diagnostic_file"] = f"diagnostics/{name}"
                    self.unknown_shots += 1
                    self.last_unknown_shot_ms = unix_ms
                    await asyncio.to_thread(self._write_diagnostic, packet[12:], name)
                self.rows.append(row)
                if len(self.rows) > MAX_TEST_ROWS:
                    self.rows = self.rows[-MAX_TEST_ROWS:]
                await asyncio.to_thread(self._append_test_row, row)
                if self.save_images:
                    await asyncio.to_thread(self._write_image, result["image"], f"test-{row['observation_id']}.jpg")
                    row["image_file"] = f"images/test-{row['observation_id']}.jpg"
            elif testing and identity in identities:
                self.skipped_reference_frames += 1
            self.cond.notify_all()
        guided = self.guided
        if guided is not None:
            await guided.ingest(result, unix_ms)

    def _test_row(self, unix_ms, result, decision, ground_truth, camera, mask, save_images, segment) -> dict:
        predicted = decision["predicted"]
        stable = decision.get("stable_predicted") or "UNKNOWN"
        observation_id = uuid.uuid4().hex[:12]
        image_file = f"images/test-{observation_id}.jpg" if save_images else ""
        return self._row(
            unix_ms=unix_ms,
            capture_unix_ms=int(result["capture_unix_ms"]),
            row_type="test",
            camera=camera,
            ground_truth=ground_truth,
            reference_place="",
            predicted=predicted,
            outcome=outcome_for(ground_truth, predicted),
            decision=decision,
            feature_count=int(result["feature_count"]),
            mask=mask,
            match_ms=decision["match_ms"],
            phone_sequence=int(result["sequence"]),
            observation_id=observation_id,
            image_file=image_file,
            raw_predicted=predicted,
            stable_predicted=stable,
            stable_support=decision.get("stable_support", 0),
            stable_counts=decision.get("stable_counts") or _empty_counts(),
            stable_outcome=outcome_for_stable(ground_truth, stable),
            event="",
            segment=segment,
            failure=decision.get("failure", ""),
            best_reference_id=decision.get("best_reference_id", ""),
            blur_score=decision.get("blur_score", ""),
            interval="",
            diagnostic_file="",
        )

    def _row(
        self,
        *,
        unix_ms,
        capture_unix_ms,
        row_type,
        camera,
        ground_truth,
        reference_place,
        predicted,
        outcome,
        decision,
        feature_count,
        mask,
        match_ms,
        phone_sequence,
        observation_id,
        image_file,
        raw_predicted="",
        stable_predicted="",
        stable_support="",
        stable_counts=None,
        stable_outcome="",
        event="",
        segment="",
        failure="",
        best_reference_id="",
        blur_score="",
        interval="",
        diagnostic_file="",
    ) -> dict:
        matches = decision["matches"] if decision else _empty_counts()
        inliers = decision["inliers"] if decision else _empty_counts()
        enabled = bool(mask and mask.get("enabled"))
        return {
            "timestamp_iso": _iso(unix_ms),
            "unix_ms": unix_ms,
            "capture_unix_ms": capture_unix_ms,
            "clock_delta_ms": unix_ms - int(capture_unix_ms),
            "row_type": row_type,
            "camera": camera,
            "ground_truth": ground_truth,
            "reference_place": reference_place,
            "predicted": predicted,
            "outcome": outcome,
            "matches_A": matches["A"],
            "inliers_A": inliers["A"],
            "matches_B": matches["B"],
            "inliers_B": inliers["B"],
            "matches_C": matches["C"],
            "inliers_C": inliers["C"],
            "best_place": decision["best_place"] if decision else "",
            "best_matches": decision["best_matches"] if decision else 0,
            "best_inliers": decision["best_inliers"] if decision else 0,
            "second_place": decision["second_place"] if decision else "",
            "second_inliers": decision["second_inliers"] if decision else 0,
            "feature_count": feature_count,
            "masked": int(enabled),
            "mask_x": round(float(mask["x"]), 4) if enabled else "",
            "mask_y": round(float(mask["y"]), 4) if enabled else "",
            "mask_w": round(float(mask["w"]), 4) if enabled else "",
            "mask_h": round(float(mask["h"]), 4) if enabled else "",
            "match_ms": match_ms,
            "phone_sequence": phone_sequence,
            "observation_id": observation_id,
            "image_file": image_file,
            "raw_predicted": raw_predicted,
            "stable_predicted": stable_predicted,
            "stable_support": stable_support,
            "stable_window_A": "" if not stable_counts else stable_counts["A"],
            "stable_window_B": "" if not stable_counts else stable_counts["B"],
            "stable_window_C": "" if not stable_counts else stable_counts["C"],
            "stable_outcome": stable_outcome,
            "event": event,
            "segment": segment,
            "failure": failure,
            "best_reference_id": best_reference_id,
            "blur_score": blur_score,
            "interval": interval,
            "diagnostic_file": diagnostic_file,
        }

    def _write_image(self, image, name: str) -> None:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(self.image_dir / name), image)

    def _write_features(self, observation_id: str, keypoints, descriptors) -> None:
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        arrays = _keypoints_to_arrays(keypoints)
        if descriptors is None:
            arrays["descriptors"] = np.zeros((0, 32), dtype=np.uint8)
        else:
            arrays["descriptors"] = np.ascontiguousarray(descriptors)
        np.savez_compressed(self.feature_dir / f"{observation_id}.npz", **arrays)

    def _inside_dir(self, path: Path, folder: Path) -> bool:
        try:
            path.resolve().relative_to(folder.resolve())
        except ValueError:
            return False
        return True

    def _read_reference_image(self, image_file: str):
        if not image_file:
            return None
        path = (self.root / image_file).resolve()
        if not self._inside_dir(path, self.image_dir) or not path.is_file():
            return None
        return cv2.imread(str(path), cv2.IMREAD_COLOR)

    def _file_newer(self, path: Path, saved_unix_ms: int) -> bool:
        try:
            modified_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            return False
        return modified_ms > int(saved_unix_ms) + 120_000

    def _build_audit(self) -> dict:
        """Check that each saved reference still matches the index. This does not rewrite files."""
        items = []
        problems = list(self.missing_features)
        known = set()
        with_photo = 0
        descriptors_only = 0
        for item in self.references:
            known.add(item["id"])
            feature_path = self.feature_dir / f"{item['id']}.npz"
            loaded = 0 if item.get("descriptors") is None else int(len(item["descriptors"]))
            listed = int(item.get("descriptor_count") or 0)
            keypoints = len(item.get("keypoints") or [])
            descriptors_match = (
                feature_path.is_file()
                and loaded == listed
                and keypoints == int(item.get("feature_count") or 0)
            )
            image_rel = item.get("image_file") or ""
            photo = False
            photo_ok = False
            newer = False
            if image_rel:
                photo_path = (self.root / image_rel).resolve()
                if self._inside_dir(photo_path, self.image_dir) and photo_path.is_file():
                    image = item.get("image")
                    photo = image is not None and photo_path.stat().st_size > 0
                    if image is not None:
                        height, width = image.shape[:2]
                        photo_ok = width == int(item.get("width") or 0) and height == int(item.get("height") or 0)
                    newer = self._file_newer(photo_path, int(item.get("saved_unix_ms") or 0))
                else:
                    problems.append(f"{item['place']} {item['id']} lists a photo that is not on disk")
            if not descriptors_match:
                problems.append(f"{item['place']} {item['id']} descriptors do not match the index")
            if image_rel and not photo_ok:
                problems.append(f"{item['place']} {item['id']} photo does not match the saved size")
            if newer or (feature_path.is_file() and self._file_newer(feature_path, int(item.get("saved_unix_ms") or 0))):
                problems.append(f"{item['place']} {item['id']} file is newer than the time it was saved")
            if photo:
                with_photo += 1
            else:
                descriptors_only += 1
            items.append(
                {
                    "id": item["id"],
                    "place": item["place"],
                    "feature_count": int(item.get("feature_count") or 0),
                    "descriptor_count": listed,
                    "descriptors_match": descriptors_match,
                    "photo": photo,
                    "saved_unix_ms": int(item.get("saved_unix_ms") or 0),
                }
            )
        orphans = 0
        if self.feature_dir.exists():
            orphans += sum(1 for path in self.feature_dir.glob("*.npz") if path.stem not in known)
        if self.image_dir.exists():
            orphans += sum(1 for path in self.image_dir.glob("ref-*.jpg") if path.stem[4:] not in known)
        if orphans:
            problems.append(f"{orphans} files are on disk and not in the index")
        return {
            "intact": not problems,
            "problems": problems,
            "items": items,
            "with_photo": with_photo,
            "descriptors_only": descriptors_only,
            "orphans": orphans,
        }

    def _write_diagnostic(self, payload: bytes, name: str) -> None:
        self.diagnostic_dir.mkdir(parents=True, exist_ok=True)
        (self.diagnostic_dir / name).write_bytes(payload)

    def reference_jpeg(self, reference_id: str) -> bytes | None:
        if len(reference_id) != 12 or any(char not in "0123456789abcdef" for char in reference_id):
            return None
        if not any(item["id"] == reference_id for item in self.references):
            return None
        path = (self.image_dir / f"ref-{reference_id}.jpg").resolve()
        if not self._inside_dir(path, self.image_dir) or not path.is_file():
            return None
        return path.read_bytes()

    def _session_span(self) -> dict | None:
        stamps = []
        for row in self.rows:
            if row.get("row_type") != "test":
                continue
            stamp = _unix_ms(row)
            if stamp is not None:
                stamps.append(stamp)
        if not stamps:
            return None
        return {"start_unix_ms": min(stamps), "end_unix_ms": max(stamps), "frames": len(stamps)}

    async def save_reference(self, place: str, camera: str, save_images: bool) -> dict:
        if place not in PLACES:
            raise ValueError("place must be A, B, or C")
        if camera not in CAMERAS:
            raise ValueError("camera must be palm or outward")
        async with self.cond:
            if self.testing:
                raise RuntimeError("Stop the test walk before saving another reference.")
            result = self.latest
            mask = dict(self.mask) if self.mask else None
            if result is None:
                raise RuntimeError("No camera frame has arrived yet.")
            count = sum(1 for item in self.references if item["place"] == place)
            if count >= MAX_REFERENCES_PER_PLACE:
                raise RuntimeError(f"Place {place} already has {MAX_REFERENCES_PER_PLACE} references.")
            observation_id = uuid.uuid4().hex[:12]
            keypoints = list(result["keypoints"])
            descriptors = None if result["descriptors"] is None else result["descriptors"].copy()
            image = result["image"].copy()
            record = {
                "id": observation_id,
                "place": place,
                "camera": camera,
                "saved_unix_ms": _now_ms(),
                "capture_unix_ms": int(result["capture_unix_ms"]),
                "phone_sequence": int(result["sequence"]),
                "feature_count": int(result["feature_count"]),
                "descriptor_count": 0 if descriptors is None else int(len(descriptors)),
                "width": int(result["width"]),
                "height": int(result["height"]),
                "image_file": f"images/ref-{observation_id}.jpg" if save_images else "",
                "masked": bool(mask and mask.get("enabled")),
                "keypoints": keypoints,
                "descriptors": descriptors,
                "image": image,
            }
            self.references.append(record)
        await asyncio.to_thread(self._write_features, observation_id, keypoints, descriptors)
        if save_images:
            await asyncio.to_thread(self._write_image, image, f"ref-{observation_id}.jpg")
        with self._lock:
            self._write_index()
            self.audit = self._build_audit()
        log.info("Saved reference %s for place %s", observation_id, place)
        return await self.snapshot()

    async def clear_place(self, place: str) -> dict:
        if place not in PLACES and place != "ALL":
            raise ValueError("place must be A, B, C, or ALL")
        async with self.cond:
            if self.testing:
                raise RuntimeError("Stop the test walk before clearing references.")
            removed = []
            kept = []
            for item in self.references:
                if place == "ALL" or item["place"] == place:
                    removed.append(item)
                else:
                    kept.append(item)
            self.references = kept
        for item in removed:
            feature_path = self.feature_dir / f"{item['id']}.npz"
            image_path = self.image_dir / f"ref-{item['id']}.jpg"
            feature_path.unlink(missing_ok=True)
            image_path.unlink(missing_ok=True)
        with self._lock:
            self._write_index()
            self.audit = self._build_audit()
        return await self.snapshot()

    async def set_mask(self, enabled: bool, x: float, y: float, w: float, h: float) -> dict:
        if enabled:
            for value in (x, y, w, h):
                if value < 0 or value > 1:
                    raise ValueError("mask values must be between 0 and 1")
            if w < 0.02 or h < 0.02:
                raise ValueError("mask rectangle is too small")
            mask = {"enabled": True, "x": x, "y": y, "w": w, "h": h}
        else:
            mask = None
        async with self.cond:
            self.mask = mask
        return await self.snapshot()

    def _guided_running(self) -> bool:
        guided = self.guided
        return bool(guided is not None and getattr(guided, "active", False))

    async def start_diagnostic(self, camera: str, save_unknown: bool) -> dict:
        """Log every frame without arrival buttons. Notes are added after stopping."""
        if camera not in CAMERAS:
            raise ValueError("camera must be palm or outward")
        async with self.cond:
            if self._guided_running():
                raise RuntimeError("A guided experiment is running. Stop it on the phone before starting another recording.")
            if self.testing:
                raise RuntimeError("A recording is already running.")
            if not any(item["place"] == "A" for item in self.references):
                raise RuntimeError("Save at least one reference for place A before recording.")
            self.last_archive = await asyncio.to_thread(self._archive_log)
            self.testing = True
            self.diagnostic = True
            self.save_unknown = bool(save_unknown)
            self.unknown_shots = 0
            self.last_unknown_shot_ms = 0
            self.ground_truth = ""
            self.segment = ""
            self.raw_history = []
            self.extra_presses = 0
            self.camera = camera
            self.save_images = False
            unix_ms = _now_ms()
            row = self._event_row(unix_ms, "START_DIAGNOSTIC", "")
            self.rows.append(row)
            self.last_mark = {
                "event": "START_DIAGNOSTIC",
                "repeat": False,
                "extra_presses": 0,
                "unix_ms": unix_ms,
                "segment": "",
            }
            await asyncio.to_thread(self._append_test_row, row)
            return self._public_state()

    async def set_save_unknown(self, enabled: bool) -> dict:
        async with self.cond:
            self.save_unknown = bool(enabled)
            return self._public_state()

    async def annotate_intervals(self, intervals: list[dict]) -> dict:
        """Label frames after the recording. Blank rows stay unlabeled."""
        cleaned = []
        for item in intervals:
            label = item.get("label")
            if label not in INTERVAL_LABELS:
                raise ValueError("interval must be same_spot, arm_only, or returned")
            start = int(item["start_unix_ms"])
            end = int(item["end_unix_ms"])
            if end <= start:
                raise ValueError("Each note needs an end time after its start time.")
            cleaned.append((label, start, end))
        cleaned.sort(key=lambda item: item[1])
        for index, (_label, start, _end) in enumerate(cleaned):
            if index and start < cleaned[index - 1][2]:
                raise ValueError("The notes overlap. Each moment can have one label.")
        async with self.cond:
            if self.testing:
                raise RuntimeError("Stop the recording before adding notes.")
            for row in self.rows:
                if row.get("row_type") != "test" or row.get("segment"):
                    continue
                row["interval"] = ""
                row["ground_truth"] = "UNLABELED"
                row["outcome"] = "unlabeled"
                row["stable_outcome"] = "unlabeled"
            for label, start, end in cleaned:
                for row in self.rows:
                    if row.get("row_type") != "test" or row.get("segment"):
                        continue
                    stamp = _unix_ms(row)
                    if stamp is None or stamp < start or stamp >= end:
                        continue
                    row["interval"] = label
                    row["ground_truth"] = "A"
                    row["outcome"] = outcome_for("A", row.get("predicted") or "UNKNOWN")
                    row["stable_outcome"] = outcome_for_stable("A", row.get("stable_predicted") or "UNKNOWN")
            if self.rows:
                await asyncio.to_thread(self._rewrite_tests)
            return self._public_state()

    async def start_test(self, camera: str, save_images: bool) -> dict:
        if camera not in CAMERAS:
            raise ValueError("camera must be palm or outward")
        async with self.cond:
            if self._guided_running():
                raise RuntimeError("A guided experiment is running. Stop it on the phone before starting another test.")
            if self.testing:
                raise RuntimeError("A test is already running.")
            if not self.references:
                raise RuntimeError("Save at least one reference before starting a test.")
            self.last_archive = await asyncio.to_thread(self._archive_log)
            self.testing = True
            self.diagnostic = False
            self.save_unknown = False
            self.unknown_shots = 0
            self.ground_truth = ""
            self.segment = ""
            self.raw_history = []
            self.extra_presses = 0
            self.camera = camera
            self.save_images = save_images
            unix_ms = _now_ms()
            row = self._event_row(unix_ms, "START_TEST", "")
            self.rows.append(row)
            self.last_mark = {"event": "START_TEST", "repeat": False, "extra_presses": 0, "unix_ms": unix_ms, "segment": ""}
            await asyncio.to_thread(self._append_test_row, row)
            return self._public_state()

    async def mark_event(self, event: str) -> dict:
        if event not in MARK_EVENTS:
            raise ValueError("event must be LEFT_SAVED_LOCATION, ARRIVED_A, ARRIVED_B, or ARRIVED_C")
        async with self.cond:
            if not self.testing:
                raise RuntimeError("Start the test before marking a location.")
            segment = MARK_EVENTS[event]
            repeat = self.segment == segment
            if repeat:
                self.extra_presses += 1
                logged = f"{event}_REPEAT"
            else:
                self.segment = segment
                self.ground_truth = self.segment
                self.extra_presses = 0
                logged = event
            unix_ms = _now_ms()
            row = self._event_row(unix_ms, logged, self.segment)
            self.rows.append(row)
            self.last_mark = {
                "event": logged,
                "repeat": repeat,
                "extra_presses": self.extra_presses,
                "unix_ms": unix_ms,
                "segment": self.segment,
            }
            await asyncio.to_thread(self._append_test_row, row)
            return self._public_state()

    async def stop_test(self) -> dict:
        async with self.cond:
            if self.testing:
                unix_ms = _now_ms()
                event_name = "END_DIAGNOSTIC" if self.diagnostic else "END_TEST"
                row = self._event_row(unix_ms, event_name, self.segment)
                self.rows.append(row)
                self.last_mark = {
                    "event": "END_TEST",
                    "repeat": False,
                    "extra_presses": 0,
                    "unix_ms": unix_ms,
                    "segment": self.segment,
                }
                await asyncio.to_thread(self._append_test_row, row)
            self.testing = False
            self.diagnostic = False
            self.save_images = False
            self.save_unknown = False
            return self._public_state()

    def _event_row(self, unix_ms: int, event: str, segment: str) -> dict:
        truth = segment if segment in (*PLACES, AWAY) else ""
        return self._row(
            unix_ms=unix_ms,
            capture_unix_ms=unix_ms,
            row_type="event",
            camera=self.camera,
            ground_truth=truth,
            reference_place="",
            predicted="",
            outcome="",
            decision=None,
            feature_count="",
            mask=self.mask,
            match_ms="",
            phone_sequence="",
            observation_id=uuid.uuid4().hex[:12],
            image_file="",
            event=event,
            segment=segment,
        )

    async def clear_tests(self) -> dict:
        async with self.cond:
            if self.testing:
                raise RuntimeError("Stop the test walk before clearing its log.")
            self.rows = []
            self.skipped_reference_frames = 0
        if self.test_path.exists():
            self.test_path.unlink()
        return await self.snapshot()

    def _counts(self) -> dict:
        tests = [row for row in self.rows if row.get("row_type") == "test"]
        correct = sum(1 for row in tests if row["outcome"] == "correct")
        incorrect = sum(1 for row in tests if row["outcome"] == "incorrect")
        unknown = sum(1 for row in tests if row["outcome"] == "unknown")
        hand_named = sum(1 for row in tests if row["ground_truth"] == "HAND" and row["predicted"] in PLACES)
        unfamiliar_named = sum(
            1 for row in tests if row["ground_truth"] == "UNFAMILIAR" and row["predicted"] in PLACES
        )
        known_unknown = sum(1 for row in tests if row["ground_truth"] in PLACES and row["predicted"] == "UNKNOWN")
        labels = ["A", "B", "C", AWAY, "UNFAMILIAR", "HAND"]
        columns = list(PLACES) + ["UNKNOWN"]
        matrix = {label: {column: 0 for column in columns} for label in labels}
        for row in tests:
            truth = row["ground_truth"]
            predicted = row["predicted"]
            if truth in matrix and predicted in matrix[truth]:
                matrix[truth][predicted] += 1
        by_camera: dict[str, dict[str, int]] = {}
        for camera in CAMERAS:
            subset = [row for row in tests if row["camera"] == camera]
            by_camera[camera] = {
                "frames": len(subset),
                "correct": sum(1 for row in subset if row["outcome"] == "correct"),
                "incorrect": sum(1 for row in subset if row["outcome"] == "incorrect"),
                "unknown": sum(1 for row in subset if row["outcome"] == "unknown"),
            }
        failure_names = (
            FAILURE_ZERO,
            FAILURE_MATCHES,
            FAILURE_GEOMETRY,
            FAILURE_INLIERS,
            FAILURE_MARGIN,
            FAILURE_NAMED,
        )
        failures = {name: 0 for name in failure_names}
        intervals = {label: 0 for label in INTERVAL_LABELS}
        for row in tests:
            failure = row.get("failure") or ""
            if failure in failures:
                failures[failure] += 1
            interval = row.get("interval") or ""
            if interval in intervals:
                intervals[interval] += 1
        return {
            "test_frames": len(tests),
            "correct": correct,
            "incorrect": incorrect,
            "unknown": unknown,
            "hand_named_as_place": hand_named,
            "unfamiliar_named_as_place": unfamiliar_named,
            "known_place_left_unknown": known_unknown,
            "confusion": matrix,
            "by_camera": by_camera,
            "skipped_reference_frames": self.skipped_reference_frames,
            "failures": failures,
            "intervals": intervals,
        }

    def _public_state(self) -> dict:
        counts = {place: sum(1 for item in self.references if item["place"] == place) for place in PLACES}
        decision = None
        if self.decision is not None:
            stable_counts = self.decision.get("stable_counts") or _empty_counts()
            decision = {
                "predicted": self.decision["predicted"],
                "stable_predicted": self.decision.get("stable_predicted", "UNKNOWN"),
                "stable_support": self.decision.get("stable_support", 0),
                "stable_counts": stable_counts,
                "matches": self.decision["matches"],
                "inliers": self.decision["inliers"],
                "best_place": self.decision["best_place"],
                "best_matches": self.decision["best_matches"],
                "best_inliers": self.decision["best_inliers"],
                "second_place": self.decision["second_place"],
                "second_inliers": self.decision["second_inliers"],
                "match_ms": self.decision["match_ms"],
                "failure": self.decision.get("failure") or FAILURE_NONE,
                "failure_text": FAILURE_TEXT.get(self.decision.get("failure") or FAILURE_NONE, ""),
                "by_place": self.decision.get("by_place") or {},
                "best_reference_id": self.decision.get("best_reference_id") or "",
                "blur_score": self.decision.get("blur_score", ""),
                "feature_count": self.decision.get("feature_count", ""),
            }
        return {
            "references": counts,
            "testing": self.testing,
            "ground_truth": self.ground_truth,
            "segment": self.segment,
            "camera": self.camera,
            "save_images": self.save_images,
            "mask": self.mask,
            "decision": decision,
            "clock_delta_ms": self.clock_delta_ms,
            "min_inliers": MIN_INLIERS,
            "inlier_margin": INLIER_MARGIN,
            "stable_window": STABLE_WINDOW,
            "stable_required": STABLE_REQUIRED,
            "lowe_ratio": LOWE_RATIO,
            "summary": self._counts(),
            "arrival": arrival_metrics(self.rows),
            "last_mark": self.last_mark,
            "last_archive": self.last_archive,
            "diagnostic": self.diagnostic,
            "save_unknown": self.save_unknown,
            "unknown_shots": self.unknown_shots,
            "unknown_shot_limit": MAX_UNKNOWN_SHOTS,
            "audit": self.audit,
            "session": self._session_span(),
        }

    async def snapshot(self) -> dict:
        async with self.cond:
            return self._public_state()

    async def export_rows(self) -> list[dict]:
        async with self.cond:
            return list(self.rows)

    async def next_packet(self, seen: int, timeout: float) -> tuple[bytes, int] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cond:
            while self.version == seen or self.packet is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=remaining)
                except TimeoutError:
                    if self.version == seen or self.packet is None:
                        return None
                    break
            assert self.packet is not None
            return self.packet, self.version

    def csv_text(self, rows: list[dict]) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in CSV_FIELDS} for row in rows)
        return buffer.getvalue()
