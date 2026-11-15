"""Ring return test: does the ring camera recognize a saved location when I come back?

    .\\.venv\\Scripts\\python.exe scripts\\ring_return_test.py

You save a few reference frames at one location, walk away, come back, and the
window shows whether the live ring camera frames match the saved ones.

What it reuses, unchanged:
  app/analysis.py   analyze_packet   ORB features and blur score for one frame
  app/places.py     recognize        match against the references, 12-inlier rule
  app/places.py     stable_from_raw  "confirmed" = 3 of the last 10 frames named
  firmware/xiao_sense_usb_motor/framing.py   the serial frame reader

What it does not do:
  It does not estimate distance or direction. A frame either looks like the
  saved location or it is UNKNOWN. Inlier counts are counts, not a probability.
  It never writes to the serial port, so it cannot flash the board or run the
  motor. The port is opened with DTR and RTS off.
  It does not touch recordings/places, the saved A, B, C references, or the web app.

Each run writes a new folder under recordings/ring_tests/:
  frames.csv    one row per analysed frame (at most 10 per second)
  imu.csv       every IMU sample the board sent
  events.csv    button presses, saved references, board status changes
  summary.txt   the totals printed when the test ends

Only one program can hold the XIAO serial port. Close the XIAO viewer and
switch the desktop app back to the iPhone camera before starting this.
"""

from __future__ import annotations

import argparse
import csv
import math
import queue
import statistics
import struct
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE_DIR = ROOT / "firmware" / "xiao_sense_usb_motor"
for _folder in (ROOT, FIRMWARE_DIR):
    if str(_folder) not in sys.path:
        sys.path.insert(0, str(_folder))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import serial  # noqa: E402
from serial.tools import list_ports  # noqa: E402

import framing  # noqa: E402
from app.analysis import analyze_packet  # noqa: E402
from app.places import (  # noqa: E402
    AWAY,
    MIN_INLIERS,
    STABLE_REQUIRED,
    STABLE_WINDOW,
    outcome_for,
    outcome_for_stable,
    recognize,
    stable_from_raw,
)

PLACE = "A"  # the one saved location, in the app's own naming
CONFIRMED = f"CONFIRMED {PLACE}"
PROCESS_FPS = 10  # same limit as app/xiao_source.py
LEAVING_GRACE_S = 3.0  # after "walking away" you are still standing there; not scored
APPROACH_WINDOW_S = 5.0  # named frames this close before "back" are reported separately
MIN_REFERENCE_FEATURES = 40  # same floor as app/experiment.py
MAX_REFERENCES = 10
DISPLAY_SCALE = 2
ESP_VID = 0x303A
ESP_PID = 0x1001

IMU_FIELDS = (
    "timestamp_iso", "unix_ms", "elapsed_s", "phase", "board_ms",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "accel_mag_g", "chip_temp_c",
)
FRAME_FIELDS = (
    "timestamp_iso", "unix_ms", "elapsed_s", "phase", "trip", "ground_truth", "scored",
    "serial_frame", "jpeg_bytes", "width", "height", "feature_count", "blur_score",
    "predicted", "stable_predicted", "stable_support", "outcome", "stable_outcome",
    "matches", "inliers", "min_inliers", "failure", "best_reference_id", "references",
    "analyze_ms", "match_ms",
    "imu_board_ms", "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps", "accel_mag_g",
    "imu_samples_since_last_row", "gyro_abs_max_dps_since_last_row",
    "jpeg_received_total", "jpeg_valid_total", "jpeg_invalid_total",
    "imu_valid_total", "imu_bad_total", "image_file",
)
EVENT_FIELDS = ("timestamp_iso", "unix_ms", "elapsed_s", "event", "detail")


def _iso(unix_ms: int) -> str:
    return datetime.fromtimestamp(unix_ms / 1000).isoformat(timespec="milliseconds")


def find_port() -> str:
    matches = [p.device for p in list_ports.comports() if p.vid == ESP_VID and p.pid == ESP_PID]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit("The XIAO is not on a USB serial port. Plug in the ring's USB cable. Nothing was opened.")
    raise SystemExit(f"More than one Espressif serial port is connected: {matches}. Unplug the extra board.")


def parse_status(text: str) -> dict | None:
    """Read 'status boot 1 reset POWERON up_ms 2345 ...' into a dict of strings."""
    tokens = text.split()
    if not tokens or tokens[0] != "status" or len(tokens) % 2 == 0:
        return None
    return dict(zip(tokens[1::2], tokens[2::2]))


def parse_mask(text: str) -> dict | None:
    """'x,y,w,h' as fractions of the frame, for example 0,0,0.35,1 for the left 35%."""
    if not text:
        return None
    try:
        x, y, w, h = (float(part) for part in text.split(","))
    except ValueError:
        raise SystemExit("--mask needs four numbers between 0 and 1: x,y,w,h") from None
    if min(x, y, w, h) < 0 or max(x, y, w, h) > 1 or w < 0.02 or h < 0.02:
        raise SystemExit("--mask values must be between 0 and 1, and the rectangle at least 0.02 wide and tall")
    return {"enabled": True, "x": x, "y": y, "w": w, "h": h}


def parse_script(text: str) -> list[tuple[float, str]]:
    """'save@2,away@5,back@12,end@18'. For software checks only, not for a real walk."""
    steps = []
    for part in filter(None, (item.strip() for item in text.split(","))):
        action, _, at = part.partition("@")
        if action not in {"save", "away", "back", "end"}:
            raise SystemExit(f"--script action must be save, away, back, or end, not {action!r}")
        steps.append((float(at), action))
    return sorted(steps)


class ReadOnlyPort:
    """Pass reads through and count the bytes. There is no write method."""

    def __init__(self, port) -> None:
        self._port = port
        self.bytes_read = 0

    def read(self, count: int) -> bytes:
        data = self._port.read(count)
        self.bytes_read += len(data)
        return data

    def close(self) -> None:
        self._port.close()


class Feed(threading.Thread):
    """Read every frame the board sends, count it, and keep the newest camera frame."""

    def __init__(self, port, imu_path: Path) -> None:
        super().__init__(name="ring-serial", daemon=True)
        self.port = ReadOnlyPort(port)
        self.reader = framing.FrameReader(self.port)
        self.imu_path = imu_path
        self.lock = threading.Lock()
        self.events: queue.Queue = queue.Queue()
        self.stop_flag = threading.Event()
        self.started = time.monotonic()
        self.phase = "setup"
        self.lost = ""
        self.framed_bytes = 0
        self.unframed_first_2s: int | None = None
        self.unframed_total = 0
        self.jpeg_received = 0
        self.jpeg_valid = 0
        self.jpeg_invalid = 0
        self.first_jpeg: float | None = None
        self.last_jpeg: float | None = None
        self.longest_gap = 0.0
        self.imu_valid = 0
        self.imu_bad = 0
        self.imu_ids: set[tuple[int, int]] = set()
        self.last_imu: dict | None = None
        self.phase_imu: dict[str, dict] = {}
        self.text_count = 0
        self.board_text_events = 0
        self.first_status: dict | None = None
        self.last_status: dict | None = None
        self.restarts = 0
        self.reset_reasons: set[str] = set()
        self.latest: dict | None = None
        self.agg = self._new_agg()

    @staticmethod
    def _new_agg() -> dict:
        return {"count": 0, "gyro_max": 0.0}

    def _unframed(self) -> int:
        return self.port.bytes_read - self.framed_bytes - len(self.reader.buffer)

    def run(self) -> None:
        try:
            with self.imu_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, lineterminator="\n")
                writer.writerow(IMU_FIELDS)
                while not self.stop_flag.is_set():
                    try:
                        frame = self.reader.next_frame()
                    except (serial.SerialException, OSError) as error:
                        self.lost = str(error) or error.__class__.__name__
                        self.events.put(("USB_LOST", self.lost))
                        break
                    now = time.monotonic()
                    if frame is not None:
                        self.framed_bytes += framing.HEADER.size + len(frame[1]) + 4
                    if self.unframed_first_2s is None and now - self.started >= 2.0:
                        self.unframed_first_2s = self._unframed()
                    if frame is None:
                        continue
                    kind, payload = frame
                    if kind == framing.FRAME_TEXT:
                        self._text(payload)
                    elif kind == framing.FRAME_IMU:
                        self._imu(payload, now, writer)
                    else:
                        self._jpeg(payload, now)
        finally:
            self.unframed_total = self._unframed()
            if self.unframed_first_2s is None:
                self.unframed_first_2s = self.unframed_total
            try:
                self.port.close()
            except Exception:
                pass

    def _jpeg(self, payload: bytes, now: float) -> None:
        ok = len(payload) >= 4 and payload[0:2] == b"\xff\xd8"
        if ok:
            ok = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE) is not None
        with self.lock:
            self.jpeg_received += 1
            if not ok:
                self.jpeg_invalid += 1
                return
            self.jpeg_valid += 1
            if self.first_jpeg is None:
                self.first_jpeg = now
            elif self.last_jpeg is not None:
                self.longest_gap = max(self.longest_gap, now - self.last_jpeg)
            self.last_jpeg = now
            self.latest = {
                "seq": self.jpeg_valid,
                "payload": payload,
                "mono": now,
                "unix_ms": int(time.time() * 1000),
                "imu": self.last_imu,
            }

    def _imu(self, payload: bytes, now: float, writer) -> None:
        sample = framing.decode_imu(payload)
        if sample is None or not sample["scaled"]:
            with self.lock:
                self.imu_bad += 1
            return
        magnitude = math.sqrt(sample["ax_g"] ** 2 + sample["ay_g"] ** 2 + sample["az_g"] ** 2)
        gyro = max(abs(sample["gx_dps"]), abs(sample["gy_dps"]), abs(sample["gz_dps"]))
        sample["accel_mag_g"] = magnitude
        unix_ms = int(time.time() * 1000)
        with self.lock:
            self.imu_valid += 1
            self.imu_ids.add((sample["address"], sample["whoami"]))
            self.last_imu = sample
            self.agg["count"] += 1
            self.agg["gyro_max"] = max(self.agg["gyro_max"], gyro)
            phase = self.phase
            stats = self.phase_imu.setdefault(
                phase, {"count": 0, "mag_sum": 0.0, "mag_min": magnitude, "mag_max": magnitude, "gyro_max": 0.0}
            )
            stats["count"] += 1
            stats["mag_sum"] += magnitude
            stats["mag_min"] = min(stats["mag_min"], magnitude)
            stats["mag_max"] = max(stats["mag_max"], magnitude)
            stats["gyro_max"] = max(stats["gyro_max"], gyro)
        writer.writerow(
            [
                _iso(unix_ms), unix_ms, f"{now - self.started:.3f}", phase, sample["sample_ms"],
                f"{sample['ax_g']:.4f}", f"{sample['ay_g']:.4f}", f"{sample['az_g']:.4f}",
                f"{sample['gx_dps']:.2f}", f"{sample['gy_dps']:.2f}", f"{sample['gz_dps']:.2f}",
                f"{magnitude:.4f}", f"{sample['temp_c']:.1f}",
            ]
        )

    def _text(self, payload: bytes) -> None:
        text = payload.decode("utf-8", errors="replace")
        state = parse_status(text)
        with self.lock:
            self.text_count += 1
            if state is not None and state.get("up_ms", "").isdigit():
                previous = self.last_status
                if previous is None:
                    self.first_status = state
                    self.events.put(("BOARD_STATUS", text))
                elif int(state["up_ms"]) < int(previous["up_ms"]) or state.get("boot") != previous.get("boot"):
                    self.restarts += 1
                    self.events.put(("BOARD_RESTART", text))
                elif state.get("pulses") != previous.get("pulses"):
                    self.events.put(("MOTOR_PULSE_COUNT_CHANGED", text))
                self.last_status = state
                self.reset_reasons.add(state.get("reset", "?"))
                return
            if text.startswith(("sensor ", "imu MPU")):
                return
            if self.board_text_events < 200:
                self.board_text_events += 1
                self.events.put(("BOARD_TEXT", text))

    def take(self, after_seq: int) -> tuple[dict, dict] | None:
        """Newest valid camera frame, if it is newer than after_seq, with the IMU tally since the last take."""
        with self.lock:
            if self.latest is None or self.latest["seq"] <= after_seq:
                return None
            item = self.latest
            agg = self.agg
            self.agg = self._new_agg()
            return item, agg

    def counts(self) -> dict:
        with self.lock:
            span = 0.0
            if self.first_jpeg is not None and self.last_jpeg is not None:
                span = self.last_jpeg - self.first_jpeg
            return {
                "jpeg_received": self.jpeg_received,
                "jpeg_valid": self.jpeg_valid,
                "jpeg_invalid": self.jpeg_invalid,
                "camera_fps": (self.jpeg_valid - 1) / span if span > 0 and self.jpeg_valid > 1 else 0.0,
                "longest_gap_ms": self.longest_gap * 1000.0,
                "imu_valid": self.imu_valid,
                "imu_bad": self.imu_bad,
                "imu_ids": sorted(self.imu_ids),
                "last_imu": self.last_imu,
                "phase_imu": {name: dict(stats) for name, stats in self.phase_imu.items()},
                "text_count": self.text_count,
                "first_status": self.first_status,
                "last_status": self.last_status,
                "restarts": self.restarts,
                "reset_reasons": sorted(self.reset_reasons),
                "lost": self.lost,
                "unframed_first_2s": self.unframed_first_2s,
                "unframed_total": self.unframed_total,
            }


class RingTest:
    """The reference frames, the away and back marks, the scores, and the log files."""

    def __init__(self, feed: Feed, out_dir: Path, port_name: str, mask: dict | None, save_images: bool) -> None:
        self.feed = feed
        self.out_dir = out_dir
        self.port_name = port_name
        self.mask = mask
        self.save_images = save_images
        self.started_mono = feed.started
        self.started_unix_ms = int(time.time() * 1000)
        self.state = "setup"  # setup, away, returned
        self.trip = 0
        self.trips: list[dict] = []
        self.references: list[dict] = []
        self.history: list[str] = []
        self.rows: list[dict] = []
        self.last_seq = 0
        self.last_processed = 0.0
        self.last_result: dict | None = None
        self.last_item: dict | None = None
        self.last_reference_seq = 0
        self.analyze_failed = 0
        self.finished = False
        self.ended_by = ""
        self._frames_handle = (out_dir / "frames.csv").open("w", encoding="utf-8", newline="")
        self._frames = csv.DictWriter(self._frames_handle, fieldnames=FRAME_FIELDS, lineterminator="\n")
        self._frames.writeheader()
        self._events_handle = (out_dir / "events.csv").open("w", encoding="utf-8", newline="")
        self._events = csv.writer(self._events_handle, lineterminator="\n")
        self._events.writerow(EVENT_FIELDS)
        self.log_event("SESSION_START", f"port {port_name}, mask {mask or 'off'}, save_images {save_images}")

    # ---- logging -------------------------------------------------------------

    def log_event(self, event: str, detail: str = "") -> None:
        unix_ms = int(time.time() * 1000)
        elapsed = time.monotonic() - self.started_mono
        self._events.writerow([_iso(unix_ms), unix_ms, f"{elapsed:.3f}", event, detail])
        self._events_handle.flush()

    def drain_feed_events(self) -> list[tuple[str, str]]:
        drained = []
        while True:
            try:
                event, detail = self.feed.events.get_nowait()
            except queue.Empty:
                return drained
            self.log_event(event, detail)
            drained.append((event, detail))

    # ---- the three buttons ---------------------------------------------------

    def save_reference(self) -> str:
        if self.state != "setup":
            return "References can only be saved before you walk away."
        result, item = self.last_result, self.last_item
        if result is None or item is None:
            return "No camera frame has arrived yet."
        if item["seq"] == self.last_reference_seq:
            return "That frame is already saved. Wait for the next frame."
        if len(self.references) >= MAX_REFERENCES:
            return f"{MAX_REFERENCES} references are already saved."
        features = int(result["feature_count"])
        if features < MIN_REFERENCE_FEATURES or result["descriptors"] is None:
            return (
                f"Not saved. This frame has only {features} features and a reference needs "
                f"{MIN_REFERENCE_FEATURES}. Aim at something with detail and more light."
            )
        reference_id = uuid.uuid4().hex[:12]
        image_file = ""
        if self.save_images:
            folder = self.out_dir / "images"
            folder.mkdir(exist_ok=True)
            image_file = f"images/ref-{reference_id}.jpg"
            (self.out_dir / image_file).write_bytes(item["payload"])
        self.references.append(
            {
                "id": reference_id,
                "place": PLACE,
                "keypoints": list(result["keypoints"]),
                "descriptors": result["descriptors"].copy(),
                "image": result["image"].copy(),
                "feature_count": features,
                "blur_score": result["blur_score"],
            }
        )
        self.last_reference_seq = item["seq"]
        self.log_event(
            "REFERENCE_SAVED",
            f"id {reference_id}, serial_frame {item['seq']}, features {features}, "
            f"blur {result['blur_score']}, image {image_file or 'not saved'}",
        )
        return f"Reference {len(self.references)} saved ({features} features)."

    def mark_away(self) -> str:
        if not self.references:
            return "Save at least one reference first."
        if self.state == "away":
            return "Already marked as away."
        self.trip += 1
        self.state = "away"
        now = time.monotonic()
        if self.trips:
            self.trips[-1]["end_mono"] = now
        self.trips.append({"trip": self.trip, "away_mono": now, "back_mono": None, "end_mono": None})
        self.log_event("WALKING_AWAY", f"trip {self.trip}, first {LEAVING_GRACE_S:.0f} s not scored")
        return f"Trip {self.trip}: walking away. Scoring starts in {LEAVING_GRACE_S:.0f} s."

    def mark_back(self) -> str:
        if self.state != "away":
            return "Press 'I am walking away' first."
        self.state = "returned"
        self.trips[-1]["back_mono"] = time.monotonic()
        self.log_event("BACK_AT_LOCATION", f"trip {self.trip}")
        return f"Trip {self.trip}: back at the location."

    # ---- one analysed frame --------------------------------------------------

    def _phase(self, now: float) -> str:
        if self.state == "away" and now - self.trips[-1]["away_mono"] < LEAVING_GRACE_S:
            return "leaving"
        return self.state

    def step(self) -> dict | None:
        """Analyse the newest camera frame, at most PROCESS_FPS times a second."""
        if self.finished:
            return None
        now = time.monotonic()
        self.feed.phase = self._phase(now)
        if now - self.last_processed < 1.0 / PROCESS_FPS - 0.004:
            return None
        taken = self.feed.take(self.last_seq)
        if taken is None:
            return None
        item, agg = taken
        self.last_seq = item["seq"]
        self.last_processed = now
        packet = struct.pack(">IQ", item["seq"] & 0xFFFFFFFF, item["unix_ms"]) + item["payload"]
        result = analyze_packet(packet)
        if result is None:
            self.analyze_failed += 1
            return None
        self.last_result = result
        self.last_item = item
        decision = recognize(result, self.references, self.mask) if self.references else None
        predicted = ""
        stable = ""
        support = 0
        if decision is not None:
            predicted = decision["predicted"]
            self.history.append(predicted)
            self.history = self.history[-STABLE_WINDOW:]
            stable, support, _counts = stable_from_raw(self.history)
        phase = self._phase(now)
        truth = {"away": AWAY, "returned": PLACE}.get(phase, "")
        scored = bool(truth) and decision is not None
        counts = self.feed.counts()
        imu = item["imu"] or {}
        unix_ms = int(time.time() * 1000)
        image_file = ""
        if self.save_images and scored and (not self.rows or unix_ms - self._last_image_ms() >= 1000):
            folder = self.out_dir / "images"
            folder.mkdir(exist_ok=True)
            image_file = f"images/test-{item['seq']:06d}.jpg"
            (self.out_dir / image_file).write_bytes(item["payload"])

        def number(key: str, digits: int) -> str:
            return "" if key not in imu else f"{imu[key]:.{digits}f}"

        row = {
            "timestamp_iso": _iso(unix_ms),
            "unix_ms": unix_ms,
            "elapsed_s": f"{now - self.started_mono:.3f}",
            "phase": phase,
            "trip": self.trip,
            "ground_truth": truth,
            "scored": int(scored),
            "serial_frame": item["seq"],
            "jpeg_bytes": len(item["payload"]),
            "width": result["width"],
            "height": result["height"],
            "feature_count": result["feature_count"],
            "blur_score": result["blur_score"],
            "predicted": predicted,
            "stable_predicted": stable,
            "stable_support": support,
            "outcome": outcome_for(truth, predicted) if scored else "unlabeled",
            "stable_outcome": outcome_for_stable(truth, stable) if scored else "unlabeled",
            "matches": "" if decision is None else decision["best_matches"],
            "inliers": "" if decision is None else decision["best_inliers"],
            "min_inliers": MIN_INLIERS,
            "failure": "" if decision is None else decision["failure"],
            "best_reference_id": "" if decision is None else decision["best_reference_id"],
            "references": len(self.references),
            "analyze_ms": result["process_ms"],
            "match_ms": "" if decision is None else decision["match_ms"],
            "imu_board_ms": imu.get("sample_ms", ""),
            "ax_g": number("ax_g", 4),
            "ay_g": number("ay_g", 4),
            "az_g": number("az_g", 4),
            "gx_dps": number("gx_dps", 2),
            "gy_dps": number("gy_dps", 2),
            "gz_dps": number("gz_dps", 2),
            "accel_mag_g": number("accel_mag_g", 4),
            "imu_samples_since_last_row": agg["count"],
            "gyro_abs_max_dps_since_last_row": f"{agg['gyro_max']:.2f}",
            "jpeg_received_total": counts["jpeg_received"],
            "jpeg_valid_total": counts["jpeg_valid"],
            "jpeg_invalid_total": counts["jpeg_invalid"],
            "imu_valid_total": counts["imu_valid"],
            "imu_bad_total": counts["imu_bad"],
            "image_file": image_file,
        }
        row["_mono"] = now
        self.rows.append(row)
        self._frames.writerow({field: row[field] for field in FRAME_FIELDS})
        self._frames_handle.flush()
        return {
            "image": self._draw(result, decision),
            "predicted": predicted,
            "stable": stable,
            "support": support,
            "inliers": 0 if decision is None else decision["best_inliers"],
            "matches": 0 if decision is None else decision["best_matches"],
            "features": result["feature_count"],
            "blur": result["blur_score"],
            "phase": phase,
            "counts": counts,
            "gyro_max": agg["gyro_max"],
        }

    def _last_image_ms(self) -> int:
        for row in reversed(self.rows):
            if row["image_file"]:
                return int(row["unix_ms"])
        return 0

    def _draw(self, result: dict, decision: dict | None):
        canvas = result["image"].copy()
        height, width = canvas.shape[:2]
        for point in result["keypoints"]:
            cv2.circle(canvas, (int(round(point.pt[0])), int(round(point.pt[1]))), 1, (80, 220, 120), 1)
        if self.mask:
            x0 = int(self.mask["x"] * width)
            y0 = int(self.mask["y"] * height)
            x1 = int((self.mask["x"] + self.mask["w"]) * width)
            y1 = int((self.mask["y"] + self.mask["h"]) * height)
            cv2.rectangle(canvas, (x0, y0), (x1, y1), (150, 150, 150), 1)
        if decision is not None:
            for x, y in decision.get("inlier_points") or []:
                cv2.circle(canvas, (int(round(x)), int(round(y))), 4, (0, 220, 255), 1, lineType=cv2.LINE_AA)
        return cv2.resize(canvas, (width * DISPLAY_SCALE, height * DISPLAY_SCALE), interpolation=cv2.INTER_LINEAR)

    def tally(self) -> dict:
        """Running totals for the window."""
        scored = [row for row in self.rows if row["scored"]]
        away = [row for row in scored if row["phase"] == "away"]
        back = [row for row in scored if row["phase"] == "returned"]
        return {
            "away_unknown": sum(1 for row in away if row["predicted"] == "UNKNOWN"),
            "away_named": sum(1 for row in away if row["predicted"] == PLACE),
            "back_named": sum(1 for row in back if row["predicted"] == PLACE),
            "back_unknown": sum(1 for row in back if row["predicted"] == "UNKNOWN"),
        }

    # ---- the end -------------------------------------------------------------

    def finish(self, ended_by: str) -> str:
        if self.finished:
            return ""
        self.finished = True
        self.ended_by = ended_by
        end = time.monotonic()
        if self.trips and self.trips[-1]["end_mono"] is None:
            self.trips[-1]["end_mono"] = end
        self.feed.stop_flag.set()
        self.feed.join(timeout=3)
        self.drain_feed_events()
        self.log_event("END_TEST", ended_by)
        text = self.summary(end)
        (self.out_dir / "summary.txt").write_text(text, encoding="utf-8")
        self._frames_handle.close()
        self._events_handle.close()
        return text

    def summary(self, end: float) -> str:
        counts = self.feed.counts()
        lines: list[str] = []
        add = lines.append
        seconds = end - self.started_mono
        add("ReTrace ring return test")
        add(f"Session folder: {self.out_dir}")
        add(f"Started {_iso(self.started_unix_ms)}, ran {seconds:.1f} s. Ended by: {self.ended_by}.")
        add("")
        add("WHAT THIS MEASURES")
        add("  Each analysed camera frame is compared with the reference frames you saved, using ORB")
        add("  features and a geometric check. In the log, A means 'looks like the saved location'.")
        add("  It does not estimate distance or direction. Inlier counts are not a probability.")
        add("")
        add("BOARD AND SERIAL LINK")
        first, last = counts["first_status"], counts["last_status"]
        if first is None:
            add(f"  Port {self.port_name}. The board sent no status lines, so resets could not be watched.")
        else:
            add(
                f"  Port {self.port_name}. Reset reason before the test: {first.get('reset', '?')}. "
                f"Board restarts during the test: {counts['restarts']}."
            )
            add(
                f"  Motor pulses reported by the board: {first.get('pulses', '?')} at the start, "
                f"{last.get('pulses', '?')} at the end. This tool never sends anything to the board."
            )
        add(f"  USB connection lost: {counts['lost'] or 'no'}")
        add(
            f"  Camera frames received {counts['jpeg_received']}: valid {counts['jpeg_valid']}, "
            f"invalid {counts['jpeg_invalid']}. Average {counts['camera_fps']:.1f} per second. "
            f"Longest gap between frames {counts['longest_gap_ms']:.0f} ms."
        )
        ids = ", ".join(f"0x{address:02x}" for address, _whoami in counts["imu_ids"]) or "none seen"
        add(f"  IMU samples: valid {counts['imu_valid']}, bad {counts['imu_bad']}. Sensor address: {ids}.")
        first_2s = counts["unframed_first_2s"] or 0
        add(
            f"  Bytes outside valid frames: {first_2s} in the first 2 s (the stream is joined mid-frame), "
            f"{counts['unframed_total'] - first_2s} after that."
        )
        add("")
        add("RECOGNITION")
        add(
            f"  Frames analysed: {len(self.rows)} (at most {PROCESS_FPS} per second). "
            f"Frames that could not be analysed: {self.analyze_failed}."
        )
        features = ", ".join(str(item["feature_count"]) for item in self.references) or "none"
        add(f"  Reference frames saved: {len(self.references)} (features in each: {features}).")
        add(
            f"  Rules, unchanged from app/places.py: a frame is named when it has at least {MIN_INLIERS} inliers. "
            f"Confirmed means at least {STABLE_REQUIRED} of the last {STABLE_WINDOW} analysed frames were named."
        )
        if self.mask:
            add(f"  Features inside this rectangle were ignored: {self.mask}")
        if not self.trips:
            add("")
            add("  No trip was recorded. 'I am walking away' was never pressed, so nothing was scored.")
        totals = {"correct": [0, 0], "incorrect": [0, 0], "unknown_away": [0, 0], "unknown_back": [0, 0]}
        for trip in self.trips:
            number = trip["trip"]
            rows = [row for row in self.rows if row["trip"] == number and row["scored"]]
            away = [row for row in rows if row["phase"] == "away"]
            back = [row for row in rows if row["phase"] == "returned"]
            away_end = trip["back_mono"] or trip["end_mono"] or end
            add("")
            add(f"  Trip {number}")
            add(
                f"    Away for {away_end - trip['away_mono']:.1f} s. "
                f"The first {LEAVING_GRACE_S:.0f} s are not scored, because you were still leaving."
            )
            away_named = [row for row in away if row["predicted"] == PLACE]
            away_confirmed = [row for row in away if row["stable_predicted"] == CONFIRMED]
            add(
                f"      Scored frames {len(away)}: UNKNOWN {len(away) - len(away_named)} (the right answer while away), "
                f"named as the saved location {len(away_named)} (INCORRECT)."
            )
            if away_named and trip["back_mono"] is not None:
                near = sum(1 for row in away_named if trip["back_mono"] - row["_mono"] <= APPROACH_WINDOW_S)
                add(
                    f"        {near} of those {len(away_named)} were in the {APPROACH_WINDOW_S:.0f} s before you pressed Back, "
                    "when the location may already have been in view."
                )
            add(f"      Confirmed frames while away: {len(away_confirmed)}.")
            totals["incorrect"][0] += len(away_named)
            totals["incorrect"][1] += len(away_confirmed)
            totals["unknown_away"][0] += len(away) - len(away_named)
            totals["unknown_away"][1] += len(away) - len(away_confirmed)
            if trip["back_mono"] is None:
                add("    'I am back' was never pressed for this trip, so there is no return result.")
                continue
            back_end = trip["end_mono"] or end
            back_named = [row for row in back if row["predicted"] == PLACE]
            back_confirmed = [row for row in back if row["stable_predicted"] == CONFIRMED]
            add(f"    Back for {back_end - trip['back_mono']:.1f} s.")
            add(
                f"      Scored frames {len(back)}: recognized {len(back_named)} (CORRECT, {_percent(len(back_named), len(back))}), "
                f"UNKNOWN {len(back) - len(back_named)}."
            )
            add(f"      Confirmed frames: {len(back_confirmed)} ({_percent(len(back_confirmed), len(back))}).")
            if back_named:
                add(
                    f"      First recognized frame {back_named[0]['_mono'] - trip['back_mono']:.1f} s after you pressed Back."
                    + (
                        f" First confirmed frame at {back_confirmed[0]['_mono'] - trip['back_mono']:.1f} s."
                        if back_confirmed
                        else " It was never confirmed."
                    )
                )
                inliers = [int(row["inliers"]) for row in back_named]
                add(
                    f"      Inliers on recognized frames: median {statistics.median(inliers):.0f}, "
                    f"highest {max(inliers)} (a match needs {MIN_INLIERS})."
                )
            elif back:
                best = max(int(row["inliers"]) for row in back)
                add(f"      Highest inlier count on any frame: {best} (a match needs {MIN_INLIERS}).")
            totals["correct"][0] += len(back_named)
            totals["correct"][1] += len(back_confirmed)
            totals["unknown_back"][0] += len(back) - len(back_named)
            totals["unknown_back"][1] += len(back) - len(back_confirmed)
            if not back:
                verdict = "no frames were scored after you pressed Back"
            elif back_confirmed:
                verdict = "RECOGNIZED on return (confirmed)"
            elif back_named:
                verdict = "matched on some frames, and never confirmed"
            else:
                verdict = "NOT recognized on return"
            if len(away_confirmed) > 0:
                verdict += f"; also confirmed on {len(away_confirmed)} frames while away, which is a false match"
            add(f"    RESULT for trip {number}: {verdict}.")
        add("")
        add("TOTALS, frame by frame              single frame   confirmed")
        add(f"  correct (recognized after return)   {totals['correct'][0]:8d}   {totals['correct'][1]:8d}")
        add(f"  incorrect (named while away)        {totals['incorrect'][0]:8d}   {totals['incorrect'][1]:8d}")
        add(f"  UNKNOWN while away (expected)       {totals['unknown_away'][0]:8d}   {totals['unknown_away'][1]:8d}")
        add(f"  UNKNOWN after return (missed)       {totals['unknown_back'][0]:8d}   {totals['unknown_back'][1]:8d}")
        add("")
        add("IMU, motion readings only, not a position")
        add("  phase       samples   mean accel g   accel range g      largest gyro dps")
        for phase in ("setup", "leaving", "away", "returned"):
            stats = counts["phase_imu"].get(phase)
            if not stats:
                continue
            add(
                f"  {phase:<10} {stats['count']:8d}   {stats['mag_sum'] / stats['count']:12.3f}   "
                f"{stats['mag_min']:.3f} to {stats['mag_max']:.3f}   {stats['gyro_max']:12.1f}"
            )
        add("")
        add("Files: frames.csv (one row per analysed frame), imu.csv (every IMU sample), events.csv, summary.txt")
        return "\n".join(lines) + "\n"


def _percent(part: int, whole: int) -> str:
    return "n/a" if not whole else f"{100.0 * part / whole:.0f}%"


ACTIONS = {"save": "save_reference", "away": "mark_away", "back": "mark_back"}


def run_headless(test: RingTest, script: list[tuple[float, str]]) -> str:
    """No window. Runs the scripted presses, then ends. For software checks."""
    pending = list(script)
    ended_by = "script"
    while True:
        test.drain_feed_events()
        elapsed = time.monotonic() - test.started_mono
        stop = False
        while pending and pending[0][0] <= elapsed:
            _at, action = pending.pop(0)
            if action == "end":
                stop = True
                break
            print(f"[{elapsed:6.2f} s] {action}: {getattr(test, ACTIONS[action])()}", flush=True)
        if stop:
            break
        if test.feed.lost or not test.feed.is_alive():
            ended_by = f"serial link stopped: {test.feed.lost or 'reader ended'}"
            break
        if not pending:
            break
        test.step()
        time.sleep(0.004)
    return test.finish(ended_by)


def run_window(test: RingTest, script: list[tuple[float, str]]) -> str:
    import tkinter as tk

    root = tk.Tk()
    root.title("ReTrace ring return test")
    headline = tk.StringVar(value="Waiting for the ring camera")
    guide = tk.StringVar(value="Waiting for the first camera frame.")
    detail = tk.StringVar(value="")
    message = tk.StringVar(value="")
    head_label = tk.Label(root, textvariable=headline, font=("Segoe UI", 24, "bold"), fg="white", bg="#555555", padx=12, pady=8)
    head_label.pack(fill=tk.X)
    tk.Label(root, textvariable=guide, font=("Segoe UI", 13), wraplength=640, justify=tk.LEFT).pack(anchor="w", padx=8, pady=4)
    panel = tk.Label(root)
    panel.pack()
    tk.Label(root, textvariable=detail, font=("Consolas", 10), justify=tk.LEFT).pack(anchor="w", padx=8)
    tk.Label(root, textvariable=message, font=("Segoe UI", 11, "bold"), fg="#8a4b00", wraplength=640, justify=tk.LEFT).pack(anchor="w", padx=8)
    buttons = tk.Frame(root)
    buttons.pack(anchor="w", padx=8, pady=8)
    outcome = {"text": "", "closed": False}

    def press(action: str) -> None:
        if outcome["closed"]:
            return
        message.set(getattr(test, ACTIONS[action])())

    def end(reason: str) -> None:
        if outcome["closed"]:
            return
        outcome["closed"] = True
        outcome["text"] = test.finish(reason)
        root.quit()

    tk.Button(buttons, text="1. Save reference (S)", command=lambda: press("save"), width=20).pack(side=tk.LEFT, padx=2)
    tk.Button(buttons, text="2. I am walking away (A)", command=lambda: press("away"), width=22).pack(side=tk.LEFT, padx=2)
    tk.Button(buttons, text="3. I am back (B)", command=lambda: press("back"), width=16).pack(side=tk.LEFT, padx=2)
    tk.Button(buttons, text="4. End test (E)", command=lambda: end("End test button"), width=14).pack(side=tk.LEFT, padx=2)
    for key, action in (("s", "save"), ("a", "away"), ("b", "back")):
        root.bind(f"<KeyPress-{key}>", lambda _event, name=action: press(name))
        root.bind(f"<KeyPress-{key.upper()}>", lambda _event, name=action: press(name))
    root.bind("<KeyPress-e>", lambda _event: end("End test key"))
    root.bind("<KeyPress-E>", lambda _event: end("End test key"))
    root.protocol("WM_DELETE_WINDOW", lambda: end("window closed"))
    for at, action in script:
        if action == "end":
            root.after(int(at * 1000), lambda: end("script"))
        else:
            root.after(int(at * 1000), lambda name=action: press(name))

    def tick() -> None:
        if outcome["closed"]:
            return
        for event, text in test.drain_feed_events():
            if event in {"BOARD_RESTART", "USB_LOST", "MOTOR_PULSE_COUNT_CHANGED"}:
                message.set(f"{event}: {text}")
        if test.feed.lost or not test.feed.is_alive():
            end(f"serial link stopped: {test.feed.lost or 'reader ended'}")
            return
        view = test.step()
        if view is not None:
            ok, encoded = cv2.imencode(".png", view["image"])
            if ok:
                photo = tk.PhotoImage(data=encoded.tobytes())
                panel.configure(image=photo)
                panel.image = photo
            if not test.references:
                headline.set("No reference saved yet")
                head_label.configure(bg="#555555")
            elif view["stable"] == CONFIRMED:
                headline.set("RECOGNIZED: saved location")
                head_label.configure(bg="#1b7f3b")
            elif view["predicted"] == PLACE:
                headline.set("Possible match, not confirmed")
                head_label.configure(bg="#a86a00")
            else:
                headline.set("UNKNOWN")
                head_label.configure(bg="#555555")
            phase = view["phase"]
            if phase == "setup" and not test.references:
                guide.set("Step 1. Stand at your location, aim the ring camera at something with detail, and press S a few times while moving your hand slightly.")
            elif phase == "setup":
                guide.set(f"{len(test.references)} reference(s) saved. Press S for more, or press A at the moment you start walking away.")
            elif phase == "leaving":
                guide.set("Walking away. Scoring starts in a moment.")
            elif phase == "away":
                guide.set("Away. UNKNOWN is the right answer here. Press B only once you are standing at the location again.")
            else:
                guide.set("Back at the location. Hold still for about 10 seconds, then press E to end, or A to walk away again.")
            counts = view["counts"]
            tally = test.tally()
            imu = counts["last_imu"]
            imu_text = "no IMU sample yet" if imu is None else (
                f"accel {imu['accel_mag_g']:.2f} g   gyro peak {view['gyro_max']:.0f} dps"
            )
            detail.set(
                f"Inliers {view['inliers']:3d} (a match needs {MIN_INLIERS})   matches {view['matches']:3d}   "
                f"features {view['features']:3d}   blur {view['blur']}\n"
                f"Named in the last {STABLE_WINDOW} frames: {sum(1 for name in test.history if name == PLACE)} "
                f"(confirmed needs {STABLE_REQUIRED})\n"
                f"Camera frames {counts['jpeg_valid']} valid, {counts['jpeg_invalid']} invalid, "
                f"{counts['camera_fps']:.1f} per second   IMU samples {counts['imu_valid']}   {imu_text}\n"
                f"Phase {phase}, trip {test.trip}.  Away: UNKNOWN {tally['away_unknown']}, named {tally['away_named']}.  "
                f"Back: recognized {tally['back_named']}, UNKNOWN {tally['back_unknown']}.\n"
                "Inliers are not a distance or a probability. Nothing here estimates distance or direction."
            )
        root.after(10, tick)

    root.after(10, tick)
    root.mainloop()
    if not outcome["closed"]:
        outcome["text"] = test.finish("window closed")
    try:
        root.destroy()
    except tk.TclError:
        pass
    return outcome["text"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Ring return test. Read-only on the serial port.")
    parser.add_argument("--port", default="", help="serial port, for example COM5. Default: find the XIAO.")
    parser.add_argument("--mask", default="", help="ignore features in x,y,w,h (fractions), e.g. 0,0,0.35,1 for fingers on the left")
    parser.add_argument("--save-images", action="store_true", help="also save the reference JPEGs and one test frame a second")
    parser.add_argument("--script", default="", help="software check only: timed presses such as save@2,away@5,back@12,end@18")
    parser.add_argument("--headless", action="store_true", help="software check only: no window, needs --script")
    args = parser.parse_args()
    mask = parse_mask(args.mask)
    script = parse_script(args.script)
    if args.headless and not script:
        raise SystemExit("--headless needs --script")

    name = args.port or find_port()
    out_dir = ROOT / "recordings" / "ring_tests" / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Opening {name} read-only (DTR off, RTS off). Nothing is written to the board.", flush=True)
    try:
        port = framing.open_port(name)
    except serial.SerialException as error:
        print(
            f"Could not open {name}. Close the XIAO viewer, and switch the desktop app back to the iPhone camera. {error}",
            file=sys.stderr,
        )
        return 1
    out_dir.mkdir(parents=True, exist_ok=False)
    print(f"Logging to {out_dir}", flush=True)
    feed = Feed(port, out_dir / "imu.csv")
    test = RingTest(feed, out_dir, name, mask, args.save_images)
    feed.start()
    text = run_headless(test, script) if args.headless else run_window(test, script)
    print()
    print(text, flush=True)
    return 3 if feed.lost else 0


if __name__ == "__main__":
    raise SystemExit(main())
