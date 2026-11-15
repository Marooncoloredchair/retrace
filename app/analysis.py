"""Laptop-side visual measurements for incoming camera frames.

ORB and the blur score run on the JPEG the phone actually sent. Nothing here
estimates a position or builds a map. Images are not written to disk.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone

import cv2
import numpy as np

from app.hub import HEADER_BYTES, Hub

MAX_FEATURES = 500
ORB_LEVELS = 4
CSV_FIELDS = (
    "timestamp_iso",
    "unix_ms",
    "condition",
    "phone_sequence",
    "capture_unix_ms",
    "feature_count",
    "median_response",
    "max_response",
    "blur_score",
    "process_ms",
    "analysis_fps",
    "width",
    "height",
)

_detector = None


def _orb():
    global _detector
    if _detector is None:
        _detector = cv2.ORB_create(nfeatures=MAX_FEATURES, nlevels=ORB_LEVELS)
    return _detector


def _response_number(value: float) -> float:
    """Keep digits that three decimal places would turn into zero.

    ORB's keypoint response on these 320×240 frames is often below 0.0005.
    """
    return round(float(value), 8)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float(ordered[middle - 1] + ordered[middle]) / 2.0


def analyze_packet(packet: bytes) -> dict | None:
    """Measure one camera packet. Returns metrics and an annotated JPEG packet."""
    if len(packet) < HEADER_BYTES + 2:
        return None
    sequence = int.from_bytes(packet[0:4], "big")
    capture_unix_ms = int.from_bytes(packet[4:12], "big")
    started = time.perf_counter()
    encoded = np.frombuffer(packet[HEADER_BYTES:], dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        return None
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Fixed-width Laplacian so the score is comparable across resolutions (QVGA/VGA/…).
    blur_h, blur_w = gray.shape[:2]
    if blur_w != 320:
        blur_gray = cv2.resize(
            gray,
            (320, max(1, int(round(blur_h * 320 / blur_w)))),
            interpolation=cv2.INTER_AREA,
        )
    else:
        blur_gray = gray
    blur_score = float(cv2.Laplacian(blur_gray, cv2.CV_64F).var())
    keypoints, descriptors = _orb().detectAndCompute(gray, None)
    keypoints = list(keypoints or [])
    responses = [float(point.response) for point in keypoints]
    median_response = _response_number(_median(responses))
    max_response = _response_number(max(responses) if responses else 0.0)
    annotated = cv2.drawKeypoints(
        image,
        keypoints,
        None,
        color=(80, 220, 120),
        flags=cv2.DRAW_MATCHES_FLAGS_DRAW_RICH_KEYPOINTS,
    )
    ok, output = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        return None
    process_ms = (time.perf_counter() - started) * 1000.0
    annotated_packet = packet[:HEADER_BYTES] + output.tobytes()
    height, width = image.shape[:2]
    return {
        "packet": annotated_packet,
        "sequence": sequence,
        "capture_unix_ms": capture_unix_ms,
        "feature_count": len(keypoints),
        "median_response": median_response,
        "max_response": max_response,
        "blur_score": round(blur_score, 3),
        "process_ms": round(process_ms, 2),
        "width": int(width),
        "height": int(height),
        "image": image,
        "keypoints": keypoints,
        "descriptors": descriptors,
    }


class Analyzer:
    """Analyzes the newest camera frame and optionally logs the numbers."""

    def __init__(self, hub: Hub, on_frame=None) -> None:
        self.hub = hub
        self.on_frame = on_frame
        self.cond = asyncio.Condition()
        self.latest_packet: bytes | None = None
        self.version = 0
        self.metrics: dict | None = None
        self.analyzed_frames = 0
        self.analysis_times: deque[float] = deque(maxlen=60)
        self.rows: list[dict] = []
        self.recording = False
        self.condition = "palm"
        self.last_error = ""

    async def run(self) -> None:
        seen = 0
        while True:
            item = await self.hub.next_frame(seen, timeout=15)
            if item is None:
                continue
            packet, seen = item
            try:
                result = await asyncio.to_thread(analyze_packet, packet)
            except Exception as error:
                async with self.cond:
                    self.last_error = str(error)
                continue
            if result is None:
                continue
            now = time.time()
            monotonic = time.monotonic()
            async with self.cond:
                self.analysis_times.append(monotonic)
                while self.analysis_times and monotonic - self.analysis_times[0] > 1:
                    self.analysis_times.popleft()
                fps = len(self.analysis_times)
                self.latest_packet = result["packet"]
                self.version += 1
                self.analyzed_frames += 1
                self.last_error = ""
                self.metrics = {
                    "feature_count": result["feature_count"],
                    "median_response": result["median_response"],
                    "max_response": result["max_response"],
                    "blur_score": result["blur_score"],
                    "process_ms": result["process_ms"],
                    "analysis_fps": fps,
                    "width": result["width"],
                    "height": result["height"],
                    "sequence": result["sequence"],
                    "capture_unix_ms": result["capture_unix_ms"],
                    "analyzed_unix_ms": int(now * 1000),
                }
                if self.recording:
                    self.rows.append(self._row(now, fps, result))
                    if len(self.rows) > 100_000:
                        self.rows = self.rows[-100_000:]
                self.cond.notify_all()
            if self.on_frame is not None:
                try:
                    await self.on_frame(result, now)
                except Exception as error:
                    async with self.cond:
                        self.last_error = str(error)

    def _row(self, now: float, fps: int, result: dict) -> dict:
        return {
            "timestamp_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "unix_ms": int(now * 1000),
            "condition": self.condition,
            "phone_sequence": result["sequence"],
            "capture_unix_ms": result["capture_unix_ms"],
            "feature_count": result["feature_count"],
            "median_response": result["median_response"],
            "max_response": result["max_response"],
            "blur_score": result["blur_score"],
            "process_ms": result["process_ms"],
            "analysis_fps": fps,
            "width": result["width"],
            "height": result["height"],
        }

    async def next_annotated(self, seen: int, timeout: float) -> tuple[bytes, int] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cond:
            while self.version == seen or self.latest_packet is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=remaining)
                except TimeoutError:
                    if self.version == seen or self.latest_packet is None:
                        return None
                    break
            assert self.latest_packet is not None
            return self.latest_packet, self.version

    async def snapshot(self) -> dict:
        async with self.cond:
            now = time.monotonic()
            while self.analysis_times and now - self.analysis_times[0] > 1:
                self.analysis_times.popleft()
            payload = {
                "recording": self.recording,
                "condition": self.condition,
                "recorded_rows": len(self.rows),
                "analyzed_frames": self.analyzed_frames,
                "analysis_fps": len(self.analysis_times),
                "orb_features_max": MAX_FEATURES,
                "orb_levels": ORB_LEVELS,
                "last_error": self.last_error,
            }
            if self.metrics is None:
                payload["metrics"] = None
            else:
                payload["metrics"] = dict(self.metrics)
                payload["metrics"]["analysis_fps"] = len(self.analysis_times)
            return payload

    async def start_recording(self, condition: str) -> dict:
        if condition not in {"palm", "outward"}:
            raise ValueError("condition must be palm or outward")
        async with self.cond:
            if self.recording:
                raise RuntimeError("Recording is already running. Stop it before starting another condition.")
            self.condition = condition
            self.recording = True
            return {"recording": True, "condition": self.condition, "recorded_rows": len(self.rows)}

    async def stop_recording(self) -> dict:
        async with self.cond:
            self.recording = False
            return {"recording": False, "condition": self.condition, "recorded_rows": len(self.rows)}

    async def export_rows(self) -> list[dict]:
        async with self.cond:
            return list(self.rows)
