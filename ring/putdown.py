"""Detect a put-down from IMU: reach -> short stillness -> withdraw.

Thresholds live in Config. This module never writes to the ring or the disk;
the Engine records events and pins frames.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def _accel_g(sample: dict) -> float | None:
    if not sample or not sample.get("scaled"):
        return None
    return math.sqrt(sample["ax_g"] ** 2 + sample["ay_g"] ** 2 + sample["az_g"] ** 2)


def _gyro_dps(sample: dict) -> float | None:
    if not sample or not sample.get("scaled"):
        return None
    return math.sqrt(sample["gx_dps"] ** 2 + sample["gy_dps"] ** 2 + sample["gz_dps"] ** 2)


@dataclass
class PutDownEvent:
    at: float  # monotonic seconds
    unix_ms: int
    source: str  # "detected" or "marked"
    peak_accel_g: float = 0.0
    stillness_ms: float = 0.0


class PutDownDetector:
    """Small state machine over IMU samples."""

    IDLE = "idle"
    REACH = "reach"
    STILL = "still"

    def __init__(self, config) -> None:
        self.config = config
        self.state = self.IDLE
        self.reach_started: float | None = None
        self.still_started: float | None = None
        self.peak_accel = 0.0
        self.last_event_at: float | None = None

    def update(self, sample: dict, now: float, unix_ms: int) -> PutDownEvent | None:
        accel = _accel_g(sample)
        gyro = _gyro_dps(sample)
        if accel is None:
            return None
        # Deviation from ~1 g at rest.
        burst = abs(accel - 1.0)
        spinning = gyro is not None and gyro > self.config.putdown_still_gyro_dps
        cfg = self.config

        if self.state == self.IDLE:
            if burst >= cfg.putdown_reach_accel_g:
                self.state = self.REACH
                self.reach_started = now
                self.peak_accel = burst
            return None

        if self.state == self.REACH:
            self.peak_accel = max(self.peak_accel, burst)
            if now - (self.reach_started or now) > cfg.putdown_reach_max_s:
                self.state = self.IDLE
                return None
            if burst <= cfg.putdown_still_accel_g and not spinning:
                self.state = self.STILL
                self.still_started = now
            return None

        # STILL
        if burst > cfg.putdown_still_accel_g or spinning:
            # Motion again: if stillness was long enough, treat as withdraw -> event.
            still_ms = (now - (self.still_started or now)) * 1000.0
            if cfg.putdown_still_min_ms <= still_ms <= cfg.putdown_still_max_ms:
                if burst >= cfg.putdown_withdraw_accel_g:
                    return self._emit(now, unix_ms, still_ms)
            self.state = self.REACH if burst >= cfg.putdown_reach_accel_g else self.IDLE
            self.reach_started = now if self.state == self.REACH else None
            self.peak_accel = burst if self.state == self.REACH else 0.0
            self.still_started = None
            return None

        still_ms = (now - (self.still_started or now)) * 1000.0
        if still_ms > cfg.putdown_still_max_ms:
            self.state = self.IDLE
            self.still_started = None
        return None

    def _emit(self, now: float, unix_ms: int, still_ms: float) -> PutDownEvent | None:
        if self.last_event_at is not None and now - self.last_event_at < self.config.putdown_cooldown_s:
            self.state = self.IDLE
            self.still_started = None
            return None
        self.last_event_at = now
        self.state = self.IDLE
        self.still_started = None
        event = PutDownEvent(
            at=now,
            unix_ms=unix_ms,
            source="detected",
            peak_accel_g=round(self.peak_accel, 3),
            stillness_ms=round(still_ms, 1),
        )
        self.peak_accel = 0.0
        return event
