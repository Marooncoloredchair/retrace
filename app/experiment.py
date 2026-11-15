"""Voice-timed one-location experiment.

The phone speaks the cues. This module owns the clock, the fresh Location A
references, and the held-out scores. Those references are not written into the
saved A, B, and C database. A name still requires 12 geometric inliers and the
existing stability window. Counts are not a distance and not a probability.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from app.places import (
    MIN_INLIERS,
    _arrays_to_keypoints,
    _keypoints_to_arrays,
    recognize,
    stable_from_raw,
)

log = logging.getLogger("retrace.experiment")

MIN_WALK_SECONDS = 3
MAX_WALK_SECONDS = 60
DEFAULT_WALK_SECONDS = 8
MAX_REFERENCES = 10
MAX_REFERENCES_PER_VIEW = 4
MIN_REFERENCE_FEATURES = 40
REFERENCE_GAP_MS = 400
COUNTDOWN_TICK_S = 0.1
BASELINE_SESSION_ID = "e46093ebe771"
CORNER_SESSION_ID = "8160b20989a4"
RETURN_TRIALS = 3

FRAME_FIELDS = (
    "row_type",
    "session_id",
    "phase",
    "phase_role",
    "phase_started_unix_ms",
    "phase_ends_unix_ms",
    "timestamp_iso",
    "unix_ms",
    "capture_unix_ms",
    "phone_sequence",
    "feature_count",
    "blur_score",
    "predicted",
    "stable_predicted",
    "inliers_a",
    "matches_a",
    "failure",
    "held_out",
    "reference_saved",
    "late_seconds",
    "best_reference_id",
)

PHASE_SPECS = (
    ("prepare", 5, "transition", "Get ready", "Saving location A",
     "ReTrace experiment starting. Stand in your starting position. Five, four, three, two, one."),
    ("save_a", 5, "reference", "Saving location A", "Stay in the same spot",
     "Saving location A. Keep your arm in its natural resting position."),
    ("stationary", 10, "test", "Stationary", "Slowly raise your arm",
     "Reference saved. Stay in the same spot. Keep your arm relaxed."),
    ("arm_raise", 4, "test", "Raise your arm", "Lower your arm",
     "Slowly raise your arm."),
    ("arm_lower", 4, "test", "Lower your arm", "Move your arm slightly behind you",
     "Now lower your arm."),
    ("arm_behind", 4, "test", "Arm behind you", "Return your arm to its natural position",
     "Now move your arm slightly behind you."),
    ("arm_rest", 3, "test", "Arm back to rest", "Walk away",
     "Return your arm to its natural position."),
    ("walk_away", None, "test", "Walk away", "Turn around and walk back",
     "Now walk away from your starting location."),
    ("walk_back", None, "test", "Walk back", "Face the original direction",
     "Turn around. Walk back to your original starting position."),
    ("settle", 3, "transition", "Settle", "Hold your arm naturally",
     "Return to your starting spot. Face the original direction. Let your arm settle."),
    ("return_scheduled", 10, "test", "Scheduled return", "Experiment complete",
     "Hold your arm naturally. Testing whether ReTrace remembers location A."),
    ("complete", 0, "complete", "Complete", "",
     "ReTrace experiment complete. Your results have been saved."),
)

MULTI_OPENING = (
    "ReTrace experiment starting. Aim at the corner of your desk and keep it in view. "
    "Five, four, three, two, one."
)

MULTI_REFERENCE_SPECS = (
    ("save_rest", 5, "reference", "Saving the resting view", "Raise your arm slightly",
     "Saving location A. Arm relaxed. Keep the desk corner in view. Feet still."),
    ("save_raise", 4, "reference", "Saving the raised view", "Lower your arm",
     "Raise your arm slightly. Keep that same corner in view. Saving this angle."),
    ("save_lower", 4, "reference", "Saving the lowered view", "Move your arm slightly behind you",
     "Lower your arm slightly. Keep that same corner in view. Saving this angle."),
    ("save_behind", 4, "reference", "Saving the behind view", "Stay in the same spot",
     "Move your arm a little behind you. Keep that same corner in view. Saving this angle."),
)

MULTI_TEST_SPEECH = {
    "stationary": "Bring your arm back to rest. Keep the desk corner in view. Testing this view now.",
    "arm_raise": "Slowly raise your arm. Keep that same corner in view.",
    "arm_lower": "Now lower your arm. Keep that same corner in view.",
    "arm_behind": "Move your arm slightly behind you. Keep that same corner in view.",
    "arm_rest": "Return your arm to rest. Keep that same corner in view.",
    "return_scheduled": "Hold your arm naturally and keep the desk corner in view. Testing whether ReTrace remembers location A.",
}

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven",
    8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen",
    14: "fourteen", 15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty",
}


def parse_walk_seconds(value, fallback: int = DEFAULT_WALK_SECONDS) -> int:
    if value is None or value == "":
        number = fallback
    else:
        number = int(value)
    if number < MIN_WALK_SECONDS or number > MAX_WALK_SECONDS:
        raise ValueError(f"Walk time must be between {MIN_WALK_SECONDS} and {MAX_WALK_SECONDS} seconds.")
    return number


def number_word(value: int) -> str:
    if value in _NUMBER_WORDS:
        return _NUMBER_WORDS[value]
    if 21 <= value <= 59:
        tens, ones = divmod(value, 10)
        tens_word = _NUMBER_WORDS.get(tens * 10)
        ones_word = _NUMBER_WORDS.get(ones)
        if tens_word and ones_word:
            return f"{tens_word} {ones_word}"
    return str(value)


def parse_views(value) -> str:
    if value is None or value == "" or value == "single":
        return "single"
    if value in {"multi", "returns"}:
        return value
    raise ValueError("Choose one resting view, several arm positions, or three return trials.")


RETURNS_OPENING = (
    "ReTrace return trials starting. Stand at your desk and aim at the cabinet. "
    "The saved corner will not be changed. Five, four, three, two, one."
)


def return_schedule_specs() -> tuple:
    """Three walk-away-and-return trials, then one unfamiliar hold. No new references."""
    specs = [(
        "prepare", 5, "transition", "Get ready", "Trial 1 at the desk",
        RETURNS_OPENING,
    )]
    for number in range(1, RETURN_TRIALS + 1):
        word = number_word(number)
        after_return = "Unfamiliar place" if number == RETURN_TRIALS else f"Trial {number + 1} at the desk"
        specs.extend((
            (f"t{number}_desk", 5, "test", f"Trial {number} at the desk", "Walk away",
             f"Trial {word}. You are at your desk. Aim at the cabinet and hold still."),
            (f"t{number}_away", None, "test", f"Trial {number} walk away", "Walk back",
             "Walk away from your desk."),
            (f"t{number}_back", None, "test", f"Trial {number} walk back", "Settle",
             "Turn around. Walk back to your desk."),
            (f"t{number}_settle", 3, "transition", f"Trial {number} settle", "Hold at the desk",
             "Face the cabinet. Let your arm settle. This pause is not a timing result."),
            (f"t{number}_return", 10, "test", f"Trial {number} return hold", after_return,
             "Hold still at your desk. Testing whether ReTrace recognizes the cabinet."),
        ))
    specs.extend((
        ("control_go", None, "test", "Go to an unfamiliar place", "Hold still there",
         "Walk to a place where you cannot see your desk."),
        ("control_hold", 10, "test", "Unfamiliar place", "Experiment complete",
         "Stop. Hold still. This place should not show your desk."),
        ("complete", 0, "complete", "Complete", "",
         "ReTrace experiment complete. Your results have been saved."),
    ))
    return tuple(specs)


def schedule_specs(mode: str = "single") -> tuple:
    """Single view keeps the original cue list. Multi view saves four poses first."""
    mode = parse_views(mode)
    if mode == "single":
        return PHASE_SPECS
    if mode == "returns":
        return return_schedule_specs()
    prepare = PHASE_SPECS[0]
    prepare = (prepare[0], prepare[1], prepare[2], prepare[3], "Saving the resting view", MULTI_OPENING)
    tail = []
    for spec in PHASE_SPECS[2:]:
        tail.append((spec[0], spec[1], spec[2], spec[3], spec[4], MULTI_TEST_SPEECH.get(spec[0], spec[5])))
    return (prepare, *MULTI_REFERENCE_SPECS, *tail)


def reference_limit(mode: str) -> int:
    mode = parse_views(mode)
    if mode == "multi":
        return MAX_REFERENCES_PER_VIEW
    if mode == "returns":
        return 0
    return MAX_REFERENCES


def build_schedule(away_seconds: int, back_seconds: int, start_unix_ms: int | None = None, mode: str = "single") -> list[dict]:
    """Timed phases for one session. Walk durations are chosen before the clock starts."""
    if start_unix_ms is None:
        start_unix_ms = int(time.time() * 1000)
    cursor = int(start_unix_ms)
    phases = []
    for phase_id, seconds, role, title, next_title, speech in schedule_specs(mode):
        if phase_id in {"walk_away", "control_go"} or phase_id.endswith("_away"):
            duration = away_seconds
        elif phase_id == "walk_back" or phase_id.endswith("_back"):
            duration = back_seconds
        else:
            duration = int(seconds)
        spoken = speech
        if phase_id in {"walk_away", "control_go"} or phase_id.endswith("_away"):
            spoken = f"{speech} {number_word(duration).capitalize()} seconds."
        phase = {
            "id": phase_id,
            "role": role,
            "title": title,
            "next_title": next_title,
            "speech": spoken,
            "duration_ms": duration * 1000,
            "start_unix_ms": cursor,
            "end_unix_ms": cursor + duration * 1000,
        }
        phases.append(phase)
        cursor = phase["end_unix_ms"]
    return phases


def countdown_speech(phase_id: str, remaining_s: int, duration_s: int) -> str | None:
    """Spoken seconds. The opening second belongs to the phase sentence."""
    if remaining_s < 1 or remaining_s == duration_s:
        return None
    leaving = phase_id in {"walk_away", "control_go"} or phase_id.endswith("_away")
    returning = phase_id == "walk_back" or phase_id.endswith("_back")
    if leaving and remaining_s <= 8:
        return number_word(remaining_s).capitalize()
    if returning and remaining_s <= 5:
        return number_word(remaining_s).capitalize()
    return None


def _iso(unix_ms: int) -> str:
    return datetime.fromtimestamp(unix_ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (float(ordered[middle - 1]) + float(ordered[middle])) / 2.0


def _percent(part: int, whole: int) -> float | None:
    if whole <= 0:
        return None
    return round(100.0 * part / whole, 1)


def _number_stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(values),
        "min": round(float(min(values)), 2),
        "median": round(float(_median(values) or 0), 2),
        "max": round(float(max(values)), 2),
    }


def _bursts(frames: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for frame in frames:
        if frame.get("predicted") == "A":
            current.append(frame)
            continue
        if current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    bursts = []
    for group in groups:
        start = int(group[0]["unix_ms"])
        end = int(group[-1]["unix_ms"])
        bursts.append({"frames": len(group), "ms": end - start})
    return bursts


def _phase_report(phase: dict, frames: list[dict]) -> dict:
    scored = [frame for frame in frames if str(frame.get("held_out")) == "1"]
    features = [float(frame["feature_count"]) for frame in frames if frame.get("feature_count") not in ("", None)]
    inliers = [float(frame["inliers_a"]) for frame in scored if frame.get("inliers_a") not in ("", None)]
    raw_a = sum(1 for frame in scored if frame.get("predicted") == "A")
    stable_a = sum(1 for frame in scored if frame.get("stable_predicted") == "CONFIRMED A")
    unknown = sum(1 for frame in scored if frame.get("predicted") == "UNKNOWN")
    zero_features = sum(1 for frame in frames if str(frame.get("feature_count")) == "0")
    bursts = _bursts(scored)
    return {
        "id": phase["id"],
        "role": phase["role"],
        "title": phase["title"],
        "start_unix_ms": phase["start_unix_ms"],
        "end_unix_ms": phase["end_unix_ms"],
        "frames": len(frames),
        "scored_frames": len(scored),
        "raw_a": raw_a,
        "raw_a_percent": _percent(raw_a, len(scored)),
        "stable_a": stable_a,
        "stable_a_percent": _percent(stable_a, len(scored)),
        "unknown": unknown,
        "unknown_percent": _percent(unknown, len(scored)),
        "zero_feature_frames": zero_features,
        "features": _number_stats(features),
        "inliers": _number_stats(inliers),
        "bursts": len(bursts),
        "burst_lengths": bursts,
        "references_saved": sum(1 for frame in frames if str(frame.get("reference_saved")) == "1"),
    }


def _find_phase(phases: list[dict], phase_id: str) -> dict | None:
    return next((phase for phase in phases if phase["id"] == phase_id), None)


def _false_recognition_line(lookup: dict) -> str:
    """A name while walking is a false recognition: the cue said to leave the spot."""
    parts = []
    named = 0
    scored = 0
    for phase_id, label in (("walk_away", "Walking away"), ("walk_back", "Walking back")):
        report = lookup.get(phase_id)
        if report is None or report["scored_frames"] == 0:
            continue
        named += report["raw_a"]
        scored += report["scored_frames"]
        percent = report["raw_a_percent"]
        parts.append(f"{label}: {report['raw_a']} of {report['scored_frames']} frames were named A ({percent}%)")
    if not parts:
        return "No walk frames were scored, so there is no false-recognition count yet."
    joined = ". ".join(parts)
    return (
        "A name during the walk is a false recognition for this protocol, because the instruction was to leave the spot. "
        f"{joined}. Together that is {named} of {scored} walk frames. The 12-inlier rule was not changed."
    )


def _baseline_lines(lookup: dict, baseline: dict) -> list[str]:
    labels = (
        ("stationary", "Standing still"),
        ("arm_raise", "Raising the arm"),
        ("arm_lower", "Lowering the arm"),
        ("arm_behind", "Arm behind"),
        ("arm_rest", "Arm back to rest"),
        ("walk_away", "Walking away, false recognitions"),
        ("walk_back", "Walking back, false recognitions"),
        ("return_scheduled", "Scheduled return"),
    )
    lines = [
        f"Compared with the single-view session {BASELINE_SESSION_ID}. Both used the 12-inlier rule."
    ]
    for phase_id, label in labels:
        now = lookup.get(phase_id)
        then = baseline.get(phase_id)
        if now is None or then is None:
            continue
        lines.append(
            f"{label}: single-view raw A {then.get('raw_a_percent')}% of {then.get('scored_frames')} frames, "
            f"median inliers {((then.get('inliers') or {}).get('median'))}. "
            f"This session raw A {now.get('raw_a_percent')}% of {now.get('scored_frames')} frames, "
            f"median inliers {((now.get('inliers') or {}).get('median'))}."
        )
    return lines


def _baseline_phases(root: Path) -> dict | None:
    path = root / BASELINE_SESSION_ID / "session.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    phases = {item["id"]: item for item in (payload.get("summary") or {}).get("phases") or [] if item.get("id")}
    return phases or None


def _view_counts(phases: list[dict], poses: list[str]) -> list[dict]:
    counts: dict[str, int] = {}
    for pose in poses:
        if pose:
            counts[pose] = counts.get(pose, 0) + 1
    titles = {phase["id"]: phase["title"] for phase in phases}
    return [
        {"pose": pose, "title": titles.get(pose, pose), "references": count}
        for pose, count in counts.items()
    ]


def summarize(
    phases: list[dict],
    rows: list[dict],
    late_seconds: int = 0,
    reference_count: int = 0,
    mode: str = "single",
    poses: list[str] | None = None,
    baseline: dict | None = None,
) -> dict:
    """Count real frames already logged. Missing phases stay empty rather than zero-filled success."""
    frame_rows = [row for row in rows if row.get("row_type") == "frame"]
    by_phase = []
    for phase in phases:
        if phase["role"] == "complete":
            continue
        owned = [row for row in frame_rows if row.get("phase") == phase["id"]]
        by_phase.append(_phase_report(phase, owned))
    corrected = None
    return_phase = _find_phase(phases, "return_scheduled")
    if return_phase is not None and late_seconds > 0:
        start = int(return_phase["start_unix_ms"]) + int(late_seconds) * 1000
        corrected_rows = [
            row for row in frame_rows
            if row.get("phase") == "return_scheduled" and int(row["unix_ms"]) >= start
        ]
        corrected_phase = {
            **return_phase,
            "id": "return_corrected",
            "role": "test",
            "title": "After corrected arrival",
            "start_unix_ms": start,
        }
        corrected = _phase_report(corrected_phase, corrected_rows)
    lookup = {item["id"]: item for item in by_phase}
    comparison = _comparison(lookup, corrected)
    closing = comparison[-1]
    body = comparison[:-1]
    if mode == "multi" and baseline:
        body.extend(_baseline_lines(lookup, baseline))
    body.append(_false_recognition_line(lookup))
    body.append(closing)
    return {
        "mode": mode,
        "reference_count": reference_count,
        "views": _view_counts(phases, poses or []),
        "late_seconds": int(late_seconds),
        "phases": by_phase,
        "corrected_return": corrected,
        "comparison": body,
    }


def _phrase(report: dict | None, label: str) -> str:
    if report is None or report["frames"] == 0:
        return f"{label}: no frames arrived."
    if report["scored_frames"] == 0:
        return (
            f"{label}: {report['frames']} frames, {report['zero_feature_frames']} with zero ORB features. "
            "These frames were not scored as held-out recognition."
        )
    raw = report["raw_a_percent"]
    stable = report["stable_a_percent"]
    unknown = report["unknown_percent"]
    median_inliers = report["inliers"]["median"]
    return (
        f"{label}: {report['scored_frames']} scored frames, raw A {report['raw_a']} ({raw}%), "
        f"stable A {report['stable_a']} ({stable}%), UNKNOWN {report['unknown']} ({unknown}%), "
        f"median inliers {median_inliers}, zero-feature frames {report['zero_feature_frames']}, "
        f"recognition bursts {report['bursts']}."
    )


def _comparison(lookup: dict, corrected: dict | None) -> list[str]:
    lines = [
        _phrase(lookup.get("stationary"), "Standing still, before the arm moves"),
        _phrase(lookup.get("arm_raise"), "While raising the arm"),
        _phrase(lookup.get("arm_lower"), "While lowering the arm"),
        _phrase(lookup.get("arm_behind"), "With the arm slightly behind"),
        _phrase(lookup.get("arm_rest"), "After the arm returns to rest, feet unmoved"),
        _phrase(lookup.get("walk_away"), "While walking away"),
        _phrase(lookup.get("walk_back"), "While walking back"),
        _phrase(lookup.get("return_scheduled"), "Scheduled return"),
    ]
    if corrected is not None:
        lines.append(_phrase(corrected, "After the arrival time you corrected"))
    lines.append(
        "These counts compare the fresh Location A references with later frames. "
        "They are not a distance, and the scheduled return does not prove the phone reached the desk."
    )
    return lines


def _compose_pair(record: dict, reference_image: np.ndarray) -> np.ndarray:
    """Test frame on the left, the reference it was compared with on the right."""
    left = record["image"]
    height, width = left.shape[:2]
    ref_height, ref_width = reference_image.shape[:2]
    scale_x = width / ref_width
    scale_y = height / ref_height
    right = reference_image
    if ref_width != width or ref_height != height:
        right = cv2.resize(reference_image, (width, height))
    canvas = np.hstack([left, right])
    for pair in record.get("pairs") or []:
        if pair.get("inlier"):
            continue
        start = (int(round(pair["qx"])), int(round(pair["qy"])))
        end = (int(round(pair["rx"] * scale_x)) + width, int(round(pair["ry"] * scale_y)))
        cv2.line(canvas, start, end, (150, 160, 80), 1, cv2.LINE_AA)
    for pair in record.get("pairs") or []:
        if not pair.get("inlier"):
            continue
        start = (int(round(pair["qx"])), int(round(pair["qy"])))
        end = (int(round(pair["rx"] * scale_x)) + width, int(round(pair["ry"] * scale_y)))
        cv2.line(canvas, start, end, (80, 220, 120), 2, cv2.LINE_AA)
        cv2.circle(canvas, start, 3, (80, 220, 120), 1, cv2.LINE_AA)
        cv2.circle(canvas, end, 3, (80, 220, 120), 1, cv2.LINE_AA)
    caption = (
        f"{record['phase']} {record.get('role', '')}  inl {record['inliers']}  "
        f"matches {record['matches']}  {record['failure']}  ref {record['best_reference_id']}"
    )
    bar = np.full((28, canvas.shape[1], 3), 0, np.uint8)
    cv2.putText(bar, caption[:110], (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 255), 1, cv2.LINE_AA)
    return np.vstack([bar, canvas])


def _write_exemplars(folder: Path, phase_id: str, bucket: dict, refs: list[dict]) -> None:
    """Save real test photos beside the reference they matched. These are not references."""
    dest = folder / "exemplars"
    dest.mkdir(parents=True, exist_ok=True)
    by_id = {item["id"]: item for item in refs}
    manifest_path = dest / "manifest.json"
    existing = []
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
    existing = [item for item in existing if item.get("phase") != phase_id]
    for role, record in bucket.items():
        if not record or record.get("image") is None:
            continue
        record = dict(record)
        record["role"] = role
        sequence = record["phone_sequence"]
        raw_name = f"{phase_id}-{role}-{sequence}.jpg"
        cv2.imwrite(str(dest / raw_name), record["image"], [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        pair_name = ""
        reference = by_id.get(record["best_reference_id"])
        if reference is not None and reference.get("image") is not None:
            pair_name = f"{phase_id}-{role}-{sequence}-pair.jpg"
            cv2.imwrite(
                str(dest / pair_name),
                _compose_pair(record, reference["image"]),
                [int(cv2.IMWRITE_JPEG_QUALITY), 85],
            )
        existing.append({
            "role": role,
            "phase": phase_id,
            "file": raw_name,
            "pair_file": pair_name,
            "phone_sequence": sequence,
            "capture_unix_ms": record["capture_unix_ms"],
            "unix_ms": record["unix_ms"],
            "feature_count": record["feature_count"],
            "predicted": record["predicted"],
            "inliers": record["inliers"],
            "matches": record["matches"],
            "failure": record["failure"],
            "best_reference_id": record["best_reference_id"],
        })
    manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def detection_latency(phase_start: int, first_event, late_seconds: int, verified: bool) -> int | None:
    """Milliseconds from a verified arrival to the first recognition. Unverified stays empty."""
    if not verified or first_event is None:
        return None
    arrival = int(phase_start) + int(late_seconds) * 1000
    if int(first_event) < arrival:
        return None
    return int(first_event) - arrival


def _scored_frames(rows: list[dict], phase_id: str) -> list[dict]:
    return [
        row for row in rows
        if row.get("row_type") == "frame" and row.get("phase") == phase_id and str(row.get("held_out")) == "1"
    ]


def _recognition_rates(frames: list[dict]) -> dict:
    count = len(frames)
    raw = sum(1 for frame in frames if frame.get("predicted") == "A")
    stable = sum(1 for frame in frames if frame.get("stable_predicted") == "CONFIRMED A")
    unknown = sum(1 for frame in frames if frame.get("predicted") == "UNKNOWN")
    return {
        "frames": count,
        "raw_a": raw,
        "raw_a_percent": _percent(raw, count),
        "stable_a": stable,
        "stable_a_percent": _percent(stable, count),
        "unknown": unknown,
        "unknown_percent": _percent(unknown, count),
    }


def _first_unix(frames: list[dict], field: str, value: str) -> int | None:
    times = [int(frame["unix_ms"]) for frame in frames if frame.get(field) == value and frame.get("unix_ms") not in ("", None)]
    return min(times) if times else None


def trial_reports(rows: list[dict], phases: list[dict], verified: dict, late: dict) -> list[dict]:
    """Per-trial recognition. A name while leaving is recorded and is not an incorrect match."""
    reports = []
    phase_by_id = {phase["id"]: phase for phase in phases}
    for number in range(1, RETURN_TRIALS + 1):
        desk = _recognition_rates(_scored_frames(rows, f"t{number}_desk"))
        away = _recognition_rates(_scored_frames(rows, f"t{number}_away"))
        back = _recognition_rates(_scored_frames(rows, f"t{number}_back"))
        held = _scored_frames(rows, f"t{number}_return")
        returned = _recognition_rates(held)
        phase = phase_by_id.get(f"t{number}_return")
        start = 0 if phase is None else int(phase["start_unix_ms"])
        is_verified = bool(verified.get(number) or verified.get(str(number)))
        late_seconds = int(late.get(number) or late.get(str(number)) or 0)
        first_raw = _first_unix(held, "predicted", "A")
        first_stable = _first_unix(held, "stable_predicted", "CONFIRMED A")
        returned.update({
            "arrival_verified": is_verified,
            "late_seconds": late_seconds if is_verified else None,
            "raw_latency_ms": detection_latency(start, first_raw, late_seconds, is_verified),
            "stable_latency_ms": detection_latency(start, first_stable, late_seconds, is_verified),
        })
        reports.append({
            "trial": number,
            "at_desk": desk,
            "while_leaving": away,
            "while_returning": back,
            "return_hold": returned,
            "leaving_note": "Frames named A while walking away are listed here. They are not incorrect matches.",
        })
    return reports


def unfamiliar_report(rows: list[dict]) -> dict:
    """Incorrect matches are names of A during the unfamiliar hold only."""
    held = _scored_frames(rows, "control_hold")
    rates = _recognition_rates(held)
    travel = _recognition_rates(_scored_frames(rows, "control_go"))
    rates.update({
        "incorrect_raw": rates["raw_a"],
        "incorrect_stable": rates["stable_a"],
        "travel_raw_a": travel["raw_a"],
        "travel_frames": travel["frames"],
        "note": (
            "An incorrect match is a frame named A while holding still where the desk was not visible. "
            "A name while walking away, or while walking to that place, is not counted as one."
        ),
    })
    return rates


def select_equal_banks(refs: list[dict]) -> dict[str, list[dict]]:
    """Four resting views versus the first view of each pose. Same count, chosen before the test frames."""
    by_pose: dict[str, list[dict]] = {}
    for ref in refs:
        by_pose.setdefault(ref.get("pose") or "", []).append(ref)
    single = list(by_pose.get("save_rest") or [])
    multi = []
    for pose in ("save_rest", "save_raise", "save_lower", "save_behind"):
        group = by_pose.get(pose) or []
        if not group:
            raise RuntimeError(f"The saved corner is missing the {pose} view.")
        multi.append(group[0])
    if len(single) != len(multi):
        raise RuntimeError("The single-view and multi-view banks are not the same size.")
    return {"single": single, "multi": multi}


def _frame_for_match(record: dict, data) -> dict:
    return {
        "keypoints": _arrays_to_keypoints(data),
        "descriptors": data["descriptors"],
        "feature_count": int(len(data["pts"])),
        "width": int(data["width"][0]) if "width" in data else 320,
        "height": int(data["height"][0]) if "height" in data else 240,
        "sequence": int(record.get("phone_sequence") or 0),
        "capture_unix_ms": int(record.get("capture_unix_ms") or 0),
    }


def run_bank_comparison(refs: list[dict], rows: list[dict], folder: Path) -> dict:
    """Match the same held-out frames to equal-sized single-view and multi-view banks."""
    banks = select_equal_banks(refs)
    heldout = folder / "heldout"
    scored = [
        row for row in rows
        if row.get("row_type") == "frame" and str(row.get("held_out")) == "1"
    ]
    grouped: dict[str, list[dict]] = {}
    missing = 0
    for row in scored:
        path = heldout / f"{row['phase']}-{row['phone_sequence']}.npz"
        if not path.is_file():
            missing += 1
            continue
        data = np.load(path)
        grouped.setdefault(row["phase"], []).append({"row": row, "query": _frame_for_match(row, data)})
    phase_reports = []
    for phase_id, items in grouped.items():
        items.sort(key=lambda item: int(item["row"]["unix_ms"]))
        counts = {
            "single_raw": 0, "multi_raw": 0, "single_stable": 0, "multi_stable": 0, "frames": len(items),
        }
        histories = {"single": [], "multi": []}
        for item in items:
            for name, bank in banks.items():
                decision = recognize(item["query"], bank, None)
                if decision["predicted"] == "A":
                    counts[f"{name}_raw"] += 1
                stable_label, _support, _window = stable_from_raw(histories[name] + [decision["predicted"]])
                histories[name].append(decision["predicted"])
                histories[name] = histories[name][-10:]
                if stable_label == "CONFIRMED A":
                    counts[f"{name}_stable"] += 1
        count = counts["frames"]
        phase_reports.append({
            "id": phase_id,
            "frames": count,
            "single_raw_a": counts["single_raw"],
            "single_raw_percent": _percent(counts["single_raw"], count),
            "multi_raw_a": counts["multi_raw"],
            "multi_raw_percent": _percent(counts["multi_raw"], count),
            "single_stable_a": counts["single_stable"],
            "single_stable_percent": _percent(counts["single_stable"], count),
            "multi_stable_a": counts["multi_stable"],
            "multi_stable_percent": _percent(counts["multi_stable"], count),
        })
    return {
        "single_ids": [item["id"] for item in banks["single"]],
        "multi_ids": [item["id"] for item in banks["multi"]],
        "single_poses": [item.get("pose", "") for item in banks["single"]],
        "multi_poses": [item.get("pose", "") for item in banks["multi"]],
        "missing_frames": missing,
        "min_inliers": MIN_INLIERS,
        "phases": phase_reports,
        "note": (
            "Both banks contain 4 references. The single-view bank is every resting view. "
            "The multi-view bank is the first saved view of each arm position. "
            "Both see the same held-out frames. The 12-inlier rule is unchanged."
        ),
    }


def load_corner_references(root: Path) -> list[dict]:
    """Read the saved corner. This does not write into that session."""
    folder = root / CORNER_SESSION_ID
    payload = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    loaded = []
    for item in payload.get("references") or []:
        feature_path = folder / "features" / f"{item['id']}.npz"
        data = np.load(feature_path)
        image_path = folder / "images" / f"ref-{item['id']}.jpg"
        image = cv2.imread(str(image_path)) if image_path.is_file() else None
        loaded.append({
            "id": item["id"],
            "place": "A",
            "pose": item.get("pose") or "",
            "keypoints": _arrays_to_keypoints(data),
            "descriptors": np.ascontiguousarray(data["descriptors"]),
            "image": image,
            "feature_count": int(item.get("feature_count") or len(data["pts"])),
            "saved_unix_ms": item.get("saved_unix_ms", 0),
            "capture_unix_ms": item.get("capture_unix_ms", 0),
            "phone_sequence": item.get("phone_sequence", 0),
        })
    if len(loaded) != 16:
        raise RuntimeError("The saved desk corner does not have 16 references.")
    select_equal_banks(loaded)
    return loaded


def _return_lines(trials: list[dict], unfamiliar: dict, banks: dict | None) -> list[str]:
    lines = []
    for trial in trials:
        desk = trial["at_desk"]
        held = trial["return_hold"]
        away = trial["while_leaving"]
        latency = held["raw_latency_ms"]
        if not held["arrival_verified"]:
            latency_text = "Arrival was not verified, so no detection latency is reported."
        elif latency is None:
            latency_text = "Arrival was verified, and no recognition came after that time."
        else:
            latency_text = f"Verified raw detection latency {latency} ms."
        lines.append(
            f"Trial {trial['trial']}: at the desk, raw A {desk['raw_a_percent']}% of {desk['frames']} frames, "
            f"stable A {desk['stable_a_percent']}%. Return hold, raw A {held['raw_a_percent']}% of {held['frames']} frames, "
            f"stable A {held['stable_a_percent']}%. While leaving, raw A {away['raw_a']} of {away['frames']} frames. "
            f"{latency_text} Names while leaving are not incorrect matches."
        )
    lines.append(
        f"Unfamiliar hold: {unfamiliar['incorrect_raw']} of {unfamiliar['frames']} frames were named A. "
        "Those are the incorrect matches. "
        f"Stable A was {unfamiliar['incorrect_stable']} of {unfamiliar['frames']}."
    )
    if banks and banks.get("phases"):
        lines.append(banks["note"])
    elif banks is None:
        lines.append("The equal-size bank comparison is written when the trials finish.")
    lines.append(
        "The live score uses all 16 saved views. The bank comparison uses 4 resting views against "
        "4 views, one from each arm position, on the same held-out frames. Counts are not a distance."
    )
    return lines


def _safe_id(value: str) -> str:
    if len(value) != 12 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("session id is not valid")
    return value


class GuidedExperiment:
    """One automated session. Inactive until the phone presses Start test."""

    def __init__(self, root: Path, busy=None) -> None:
        self.root = root
        self.busy = busy
        self.lock = asyncio.Lock()
        self.active = False
        self.status = "idle"
        self.session_id = ""
        self.phases: list[dict] = []
        self.phase_index = -1
        self.rows: list[dict] = []
        self.refs: list[dict] = []
        self.history: list[str] = []
        self.speak = ""
        self.speak_id = 0
        self.announced_second = None
        self.late_seconds = 0
        self.summary: dict | None = None
        self.task: asyncio.Task | None = None
        self.cancel = False
        self.last_ref_ms = 0
        self.latest_decision: dict | None = None
        self.mode = "single"
        self.exemplars: dict = {}
        self.trial_late: dict = {}
        self.trial_verified: dict = {}
        self.bank_comparison: dict | None = None
        self.reference_source = ""
        self._csv_path: Path | None = None
        self._session_dir: Path | None = None
        self._corner_refs: list[dict] | None = None
        self._corner_ready = threading.Event()
        self._load_latest()
        threading.Thread(target=self._preload_corner, name="retrace-corner", daemon=True).start()

    def _preload_corner(self) -> None:
        """Read the 16 saved views before Start is pressed. This does not write them."""
        try:
            self._corner_refs = load_corner_references(self.root)
        except Exception:
            log.exception("Could not preload the saved corner")
        finally:
            self._corner_ready.set()

    def _compose_summary(self) -> dict:
        poses = [item.get("pose", "") for item in self.refs]
        baseline = _baseline_phases(self.root) if self.mode == "multi" else None
        summary = summarize(
            self.phases,
            self.rows,
            self.late_seconds,
            len(self.refs),
            self.mode,
            poses,
            baseline,
        )
        if self.mode != "returns":
            return summary
        summary["reference_source"] = CORNER_SESSION_ID
        summary["trials"] = trial_reports(self.rows, self.phases, self.trial_verified, self.trial_late)
        summary["unfamiliar"] = unfamiliar_report(self.rows)
        summary["bank_comparison"] = self.bank_comparison
        summary["comparison"] = _return_lines(summary["trials"], summary["unfamiliar"], self.bank_comparison)
        return summary

    def _phase(self) -> dict | None:
        if self.phase_index < 0 or self.phase_index >= len(self.phases):
            return None
        return self.phases[self.phase_index]

    def _cue(self, text: str) -> None:
        self.speak_id += 1
        self.speak = text

    async def start(self, away_seconds, back_seconds, views="single") -> dict:
        away = parse_walk_seconds(away_seconds)
        back = parse_walk_seconds(back_seconds)
        mode = parse_views(views)
        if self.busy and self.busy():
            raise RuntimeError("Stop the other recording before starting the guided experiment.")
        corner_refs = None
        if mode == "returns":
            if not self._corner_ready.is_set():
                await asyncio.to_thread(self._corner_ready.wait, 30)
            corner_refs = self._corner_refs
            if not corner_refs:
                corner_refs = await asyncio.to_thread(load_corner_references, self.root)
            corner_refs = list(corner_refs)
        async with self.lock:
            if self.active:
                raise RuntimeError("An experiment is already running.")
            self.session_id = uuid.uuid4().hex[:12]
            self.mode = mode
            self.phases = build_schedule(away, back, mode=mode)
            self.phase_index = -1
            self.rows = []
            self.refs = [] if corner_refs is None else corner_refs
            self.exemplars = {}
            self.trial_late = {}
            self.trial_verified = {}
            self.bank_comparison = None
            self.reference_source = CORNER_SESSION_ID if mode == "returns" else ""
            self.history = []
            self.speak = ""
            self.speak_id = 0
            self.announced_second = None
            self.late_seconds = 0
            self.summary = None
            self.cancel = False
            self.last_ref_ms = 0
            self.latest_decision = None
            self.status = "running"
            self.active = True
            self._session_dir = self.root / self.session_id
            self._csv_path = self._session_dir / "frames.csv"
            await asyncio.to_thread(self._write_session_meta)
        self.task = asyncio.create_task(self._run())
        log.info("Guided experiment %s started", self.session_id)
        return self.phone_status()

    async def stop(self) -> dict:
        async with self.lock:
            if not self.active:
                return self.phone_status()
            self.cancel = True
        if self.task is not None:
            await self.task
        return self.phone_status()

    async def correct_arrival(self, session_id: str, late_seconds: int, trial: int = 0) -> dict:
        session_id = _safe_id(session_id)
        if late_seconds < 0 or late_seconds > 30:
            raise ValueError("The correction must be from 0 to 30 seconds.")
        async with self.lock:
            if self.active:
                raise RuntimeError("Stop the experiment before correcting the arrival time.")
            if self.session_id != session_id:
                await asyncio.to_thread(self._load_session, session_id)
            if self.mode == "returns":
                if trial not in (1, 2, 3):
                    raise ValueError("Choose trial 1, 2, or 3.")
                self.trial_late[trial] = int(late_seconds)
                self.trial_verified[trial] = True
            else:
                self.late_seconds = int(late_seconds)
            self.summary = self._compose_summary()
            await asyncio.to_thread(self._write_session_meta)
            return self.desktop_status()

    async def _run(self) -> None:
        try:
            for index, phase in enumerate(self.phases):
                async with self.lock:
                    if self.cancel:
                        break
                    self.phase_index = index
                    self.history = []
                    self.announced_second = None
                    self._cue(phase["speech"])
                    self._append_phase_row(phase)
                if phase["role"] == "complete":
                    break
                while True:
                    async with self.lock:
                        if self.cancel:
                            break
                        remaining_ms = phase["end_unix_ms"] - int(time.time() * 1000)
                        remaining_s = max(0, (remaining_ms + 999) // 1000)
                        duration_s = max(1, phase["duration_ms"] // 1000)
                        spoken = countdown_speech(phase["id"], remaining_s, duration_s)
                        if spoken and self.announced_second != remaining_s:
                            self.announced_second = remaining_s
                            self._cue(spoken)
                        done = remaining_ms <= 0
                    if done:
                        break
                    await asyncio.sleep(COUNTDOWN_TICK_S)
                try:
                    await self._flush_exemplars(phase["id"])
                except Exception:
                    log.exception("Could not save exemplar frames")
                if self.cancel:
                    break
            if self.mode == "returns" and not self.cancel:
                try:
                    await self._finish_bank_comparison()
                except Exception:
                    log.exception("Could not compare the reference banks")
            async with self.lock:
                self.status = "cancelled" if self.cancel else "complete"
                if self.cancel:
                    self._cue("Experiment stopped. The partial results are on the laptop.")
                self.summary = self._compose_summary()
                self.active = False
                await asyncio.to_thread(self._write_session_meta)
        except asyncio.CancelledError:
            async with self.lock:
                self.active = False
                if self.status == "running":
                    self.status = "cancelled"
            raise
        except Exception:
            log.exception("Guided experiment failed")
            async with self.lock:
                self.status = "cancelled"
                self.active = False
                self.summary = self._compose_summary()
                await asyncio.to_thread(self._write_session_meta)
        finally:
            log.info("Guided experiment %s %s", self.session_id, self.status)

    async def ingest(self, result: dict, unix_ms: int) -> None:
        async with self.lock:
            if not self.active:
                return
            phase = self._phase()
            if phase is None or phase["role"] == "complete":
                return
            phase = dict(phase)
            refs = [
                {
                    "id": item["id"],
                    "place": "A",
                    "keypoints": item["keypoints"],
                    "descriptors": item["descriptors"],
                    "image": item.get("image"),
                }
                for item in self.refs
            ]
            history = list(self.history)
            identities = {(item["phone_sequence"], item["capture_unix_ms"]) for item in self.refs}
        identity = (int(result["sequence"]), int(result["capture_unix_ms"]))
        reference_saved = 0
        predicted = ""
        stable = ""
        inliers = ""
        matches = ""
        failure = ""
        best_reference_id = ""
        held_out = 0
        if phase["role"] == "reference":
            same_scene = True
            if refs and phase["id"] not in {"save_rest", "save_a"}:
                anchor = await asyncio.to_thread(recognize, result, refs, None)
                same_scene = int(anchor["best_inliers"]) >= MIN_INLIERS
            saved = self._capture_reference(result, unix_ms, identities, phase) if same_scene else None
            if saved is not None:
                async with self.lock:
                    limit = reference_limit(self.mode)
                    held = sum(1 for item in self.refs if item.get("pose") == saved["pose"])
                    gap_ok = not self.last_ref_ms or unix_ms - self.last_ref_ms >= REFERENCE_GAP_MS
                    if held < limit and gap_ok and self.active:
                        self.refs.append(saved)
                        self.last_ref_ms = unix_ms
                        reference_saved = 1
                if reference_saved:
                    await asyncio.to_thread(self._write_reference, saved)
        elif identity in identities:
            held_out = 0
        elif refs and phase["id"] != "prepare":
            decision = await asyncio.to_thread(recognize, result, refs, None)
            predicted = decision["predicted"]
            stable_label, _support, _counts = stable_from_raw(history + [predicted])
            stable = stable_label
            inliers = decision["inliers"]["A"]
            matches = decision["matches"]["A"]
            failure = decision["failure"]
            best_reference_id = decision.get("best_reference_id") or ""
            held_out = 1 if phase["role"] == "test" else 0
            async with self.lock:
                current = self._phase()
                if current is not None and current["id"] == phase["id"]:
                    self._remember_exemplar(phase, result, decision, unix_ms)
                    if held_out:
                        self.history.append(predicted)
                        if len(self.history) > 10:
                            self.history = self.history[-10:]
                        self.latest_decision = {
                            "predicted": predicted,
                            "stable_predicted": stable,
                            "inliers_a": inliers,
                            "failure": failure,
                        }
        elif phase["role"] == "test":
            held_out = 1
            failure = "no_references"
            predicted = "UNKNOWN"
        row = self._frame_row(
            phase, unix_ms, result, predicted, stable, inliers, matches, failure,
            held_out, reference_saved, best_reference_id,
        )
        async with self.lock:
            self.rows.append(row)
        await asyncio.to_thread(self._append_row, row)
        if self.mode == "returns" and held_out == 1:
            await asyncio.to_thread(self._write_heldout, phase["id"], result)

    def _write_heldout(self, phase_id: str, result: dict) -> None:
        if self._session_dir is None:
            return
        folder = self._session_dir / "heldout"
        folder.mkdir(parents=True, exist_ok=True)
        arrays = _keypoints_to_arrays(result["keypoints"])
        descriptors = result["descriptors"]
        if descriptors is None:
            arrays["descriptors"] = np.zeros((0, 32), dtype=np.uint8)
        else:
            arrays["descriptors"] = np.ascontiguousarray(descriptors)
        arrays["width"] = np.array([int(result["width"])], dtype=np.int32)
        arrays["height"] = np.array([int(result["height"])], dtype=np.int32)
        path = folder / f"{phase_id}-{int(result['sequence'])}.npz"
        np.savez_compressed(path, **arrays)

    async def _finish_bank_comparison(self) -> None:
        async with self.lock:
            refs = list(self.refs)
            rows = list(self.rows)
            folder = self._session_dir
        if folder is None:
            return
        comparison = await asyncio.to_thread(run_bank_comparison, refs, rows, folder)
        report_path = folder / "bank_comparison.json"
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(comparison, indent=2), encoding="utf-8")
        temporary.replace(report_path)
        async with self.lock:
            self.bank_comparison = comparison

    def _capture_reference(self, result: dict, unix_ms: int, identities: set, phase: dict) -> dict | None:
        if self.mode == "returns":
            return None
        if int(result["feature_count"]) < MIN_REFERENCE_FEATURES:
            return None
        identity = (int(result["sequence"]), int(result["capture_unix_ms"]))
        if identity in identities:
            return None
        if self.last_ref_ms and unix_ms - self.last_ref_ms < REFERENCE_GAP_MS:
            return None
        pose = phase["id"]
        limit = reference_limit(self.mode)
        if sum(1 for item in self.refs if item.get("pose") == pose) >= limit:
            return None
        descriptors = None if result["descriptors"] is None else result["descriptors"].copy()
        image = None if result["image"] is None else result["image"].copy()
        return {
            "id": uuid.uuid4().hex[:12],
            "place": "A",
            "pose": pose,
            "keypoints": list(result["keypoints"]),
            "descriptors": descriptors,
            "image": image,
            "phone_sequence": identity[0],
            "capture_unix_ms": identity[1],
            "feature_count": int(result["feature_count"]),
            "saved_unix_ms": unix_ms,
        }

    def _remember_exemplar(self, phase: dict, result: dict, decision: dict, unix_ms: int) -> None:
        """Keep a few real test frames. They are not added to the reference set."""
        image = result.get("image")
        if image is None:
            return
        inliers = int(decision["inliers"]["A"])
        matches = int(decision["matches"]["A"])
        feature_count = int(result["feature_count"])

        def pack() -> dict:
            return {
                "phase": phase["id"],
                "unix_ms": int(unix_ms),
                "phone_sequence": int(result["sequence"]),
                "capture_unix_ms": int(result["capture_unix_ms"]),
                "feature_count": feature_count,
                "predicted": decision["predicted"],
                "inliers": inliers,
                "matches": matches,
                "failure": decision["failure"],
                "best_reference_id": decision.get("best_reference_id") or "",
                "image": image.copy(),
                "pairs": [dict(pair) for pair in (decision.get("pairs") or [])],
            }

        bucket = self.exemplars.setdefault(phase["id"], {})
        best = bucket.get("best")
        if best is None or (inliers, matches) > (best["inliers"], best["matches"]):
            bucket["best"] = pack()
        if decision["predicted"] != "A" and feature_count > 0:
            miss = bucket.get("miss")
            if miss is None or feature_count > miss["feature_count"]:
                bucket["miss"] = pack()
        midpoint = (int(phase["start_unix_ms"]) + int(phase["end_unix_ms"])) / 2
        distance = abs(int(unix_ms) - midpoint)
        mid = bucket.get("mid")
        if mid is None or distance < mid["distance"]:
            chosen = pack()
            chosen["distance"] = distance
            bucket["mid"] = chosen

    def _frame_row(self, phase, unix_ms, result, predicted, stable, inliers, matches, failure, held_out, reference_saved, best_reference_id) -> dict:
        return {
            "row_type": "frame",
            "session_id": self.session_id,
            "phase": phase["id"],
            "phase_role": phase["role"],
            "phase_started_unix_ms": phase["start_unix_ms"],
            "phase_ends_unix_ms": phase["end_unix_ms"],
            "timestamp_iso": _iso(unix_ms),
            "unix_ms": unix_ms,
            "capture_unix_ms": int(result["capture_unix_ms"]),
            "phone_sequence": int(result["sequence"]),
            "feature_count": int(result["feature_count"]),
            "blur_score": result.get("blur_score", ""),
            "predicted": predicted,
            "stable_predicted": stable,
            "inliers_a": inliers,
            "matches_a": matches,
            "failure": failure,
            "held_out": held_out,
            "reference_saved": reference_saved,
            "late_seconds": self.late_seconds,
            "best_reference_id": best_reference_id,
        }

    def _append_phase_row(self, phase: dict) -> None:
        row = {field: "" for field in FRAME_FIELDS}
        row.update({
            "row_type": "phase",
            "session_id": self.session_id,
            "phase": phase["id"],
            "phase_role": phase["role"],
            "phase_started_unix_ms": phase["start_unix_ms"],
            "phase_ends_unix_ms": phase["end_unix_ms"],
            "timestamp_iso": _iso(phase["start_unix_ms"]),
            "unix_ms": phase["start_unix_ms"],
        })
        self.rows.append(row)
        self._append_row(row)

    def _append_row(self, row: dict) -> None:
        if self._csv_path is None:
            return
        self._csv_path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self._csv_path.exists()
        with self._csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FRAME_FIELDS, extrasaction="ignore", lineterminator="\n")
            if new_file:
                writer.writeheader()
            writer.writerow(row)

    def _write_reference(self, record: dict) -> None:
        if self._session_dir is None:
            return
        feature_dir = self._session_dir / "features"
        image_dir = self._session_dir / "images"
        feature_dir.mkdir(parents=True, exist_ok=True)
        arrays = _keypoints_to_arrays(record["keypoints"])
        descriptors = record["descriptors"]
        if descriptors is None:
            arrays["descriptors"] = np.zeros((0, 32), dtype=np.uint8)
        else:
            arrays["descriptors"] = np.ascontiguousarray(descriptors)
        np.savez_compressed(feature_dir / f"{record['id']}.npz", **arrays)
        if record.get("image") is not None:
            image_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(image_dir / f"ref-{record['id']}.jpg"), record["image"])

    async def _flush_exemplars(self, phase_id: str) -> None:
        async with self.lock:
            bucket = self.exemplars.pop(phase_id, None)
            refs = [
                {"id": item["id"], "image": item.get("image")}
                for item in self.refs
            ]
            folder = self._session_dir
        if not bucket or folder is None:
            return
        await asyncio.to_thread(_write_exemplars, folder, phase_id, bucket, refs)

    def _write_session_meta(self) -> None:
        if self._session_dir is None:
            return
        self._session_dir.mkdir(parents=True, exist_ok=True)
        public_refs = [
            {
                "id": item["id"],
                "feature_count": item["feature_count"],
                "saved_unix_ms": item["saved_unix_ms"],
                "capture_unix_ms": item["capture_unix_ms"],
                "phone_sequence": item["phone_sequence"],
                "pose": item.get("pose", ""),
            }
            for item in self.refs
        ]
        payload = {
            "session_id": self.session_id,
            "status": self.status,
            "mode": self.mode,
            "reference_source": self.reference_source,
            "trial_late": {str(key): value for key, value in self.trial_late.items()},
            "trial_verified": {str(key): value for key, value in self.trial_verified.items()},
            "late_seconds": self.late_seconds,
            "phases": self.phases,
            "references": public_refs,
            "summary": self.summary,
        }
        path = self._session_dir / "session.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _load_latest(self) -> None:
        if not self.root.exists():
            return
        folders = [path for path in self.root.iterdir() if (path / "session.json").is_file()]
        if not folders:
            return
        newest = max(folders, key=lambda path: (path / "session.json").stat().st_mtime)
        try:
            self._load_session(newest.name)
        except (OSError, json.JSONDecodeError, ValueError, RuntimeError):
            log.warning("Could not read the latest guided experiment")
            return
        if self.status == "running":
            self.status = "cancelled"
            self.active = False
            self.summary = self._compose_summary()
            self._write_session_meta()

    def _load_session(self, session_id: str) -> None:
        folder = self.root / _safe_id(session_id)
        path = folder / "session.json"
        csv_path = folder / "frames.csv"
        if not path.is_file():
            raise RuntimeError("That experiment session is not on this laptop.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.session_id = payload["session_id"]
        self.status = payload.get("status") or "complete"
        self.mode = payload.get("mode") or "single"
        self.reference_source = payload.get("reference_source") or ""
        self.trial_late = {int(key): int(value) for key, value in (payload.get("trial_late") or {}).items()}
        self.trial_verified = {int(key): bool(value) for key, value in (payload.get("trial_verified") or {}).items()}
        self.bank_comparison = (payload.get("summary") or {}).get("bank_comparison")
        self.exemplars = {}
        self.phases = payload.get("phases") or []
        self.late_seconds = int(payload.get("late_seconds") or 0)
        self.summary = payload.get("summary")
        self._session_dir = folder
        self._csv_path = csv_path
        self.rows = []
        if csv_path.is_file():
            with csv_path.open(encoding="utf-8", newline="") as handle:
                self.rows = list(csv.DictReader(handle))
        self.refs = [
            {
                "id": item["id"],
                "feature_count": item.get("feature_count", 0),
                "saved_unix_ms": item.get("saved_unix_ms", 0),
                "capture_unix_ms": item.get("capture_unix_ms", 0),
                "phone_sequence": item.get("phone_sequence", 0),
                "pose": item.get("pose") or "",
                "keypoints": [],
                "descriptors": None,
                "image": None,
            }
            for item in payload.get("references") or []
        ]

    def phone_status(self) -> dict:
        phase = self._phase()
        now_ms = int(time.time() * 1000)
        remaining_ms = 0
        countdown = 0
        if phase is not None and self.active:
            remaining_ms = max(0, phase["end_unix_ms"] - now_ms)
            countdown = (remaining_ms + 999) // 1000
        decision = self.latest_decision or {}
        return {
            "active": self.active,
            "status": self.status,
            "session_id": self.session_id,
            "phase": "" if phase is None else phase["id"],
            "label": "" if phase is None else phase["title"],
            "instruction": "" if phase is None else phase["speech"],
            "next_instruction": "" if phase is None else phase["next_title"],
            "speak": self.speak,
            "speak_id": self.speak_id,
            "remaining_ms": remaining_ms,
            "countdown": countdown,
            "mode": self.mode,
            "reference_count": len(self.refs),
            "predicted": decision.get("predicted", ""),
            "stable_predicted": decision.get("stable_predicted", ""),
            "inliers_a": decision.get("inliers_a", ""),
        }

    def desktop_status(self) -> dict:
        payload = self.phone_status()
        payload["summary"] = self.summary
        payload["late_seconds"] = self.late_seconds
        payload["csv_file"] = "" if self._csv_path is None else self._csv_path.name
        payload["phases"] = [
            {
                "id": phase["id"],
                "title": phase["title"],
                "role": phase["role"],
                "start_unix_ms": phase["start_unix_ms"],
                "end_unix_ms": phase["end_unix_ms"],
            }
            for phase in self.phases
            if phase["role"] != "complete"
        ]
        return payload

    async def snapshot(self, phone: bool = False) -> dict:
        async with self.lock:
            return self.phone_status() if phone else self.desktop_status()

    async def export_csv(self, session_id: str) -> str:
        session_id = _safe_id(session_id) if session_id else ""
        async with self.lock:
            if session_id and session_id != self.session_id:
                await asyncio.to_thread(self._load_session, session_id)
            rows = list(self.rows)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=FRAME_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in FRAME_FIELDS} for row in rows)
        return buffer.getvalue()

    def reference_jpeg(self, session_id: str, reference_id: str) -> bytes | None:
        try:
            session_id = _safe_id(session_id)
            reference_id = _safe_id(reference_id)
        except ValueError:
            return None
        path = (self.root / session_id / "images" / f"ref-{reference_id}.jpg").resolve()
        try:
            path.relative_to((self.root / session_id / "images").resolve())
        except ValueError:
            return None
        if not path.is_file():
            return None
        return path.read_bytes()
