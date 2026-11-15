"""Visual hold / release detector: FREE -> HOLDING -> RELEASED.

Primary put-down signal. The emitted event is the release. State is already
"holding" once cover has stayed above the pre-rise median. Samples from that
candidate stay out of the median until the rise fails, so a short history
cannot absorb the step before the one-second check. Everyday cover that never
rises above its own baseline does not fire. VLM is the final judge of whether
something was put down.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median


@dataclass
class HoldEvent:
    """Emitted on RELEASED (and for ground-truth marks with source='marked')."""

    at: float
    unix_ms: int
    source: str  # "hold" or "marked"
    grasp_unix_ms: int = 0
    hold_ms: float = 0.0
    peak_cover: float = 0.0
    baseline: float = 0.0


class HoldDetector:
    FREE = "free"
    HOLDING = "holding"

    def __init__(self, config) -> None:
        self.config = config
        self.state = self.FREE
        self.cover_history: deque[tuple[float, float]] = deque()
        self.above_since: float | None = None
        self.above_unix_ms: int = 0
        self.grasp_at: float | None = None
        self.grasp_unix_ms: int = 0
        self.hold_started: float | None = None
        self.peak_cover = 0.0
        self.baseline = 0.0
        self.baseline_at_grasp = 0.0
        self.last_event_at: float | None = None
        # Rise samples held aside until the candidate confirms or fails.
        self._pending: list[tuple[float, float]] = []

    def _prune(self, now: float) -> None:
        window = float(getattr(self.config, "hold_baseline_window_s", 30.0))
        while self.cover_history and now - self.cover_history[0][0] > window:
            self.cover_history.popleft()

    def _rolling_baseline(self) -> float:
        if not self.cover_history:
            return 0.0
        return float(median(cover for _, cover in self.cover_history))

    def _cancel_candidate(self, now: float) -> None:
        """A rejected blip joins the baseline. A confirmed rise does not."""
        if self._pending:
            self.cover_history.extend(self._pending)
            self._pending.clear()
        self.above_since = None
        self.peak_cover = 0.0
        self._prune(now)

    def update(self, cover: float, now: float, unix_ms: int, gyro_dps: float | None = None) -> HoldEvent | None:
        """cover is the 8x6 cell fraction. A fast gyro cannot start a grasp. The rise is relative to baseline."""
        cfg = self.config
        rise = float(getattr(cfg, "hold_rise_above_baseline", 0.2))
        back = float(getattr(cfg, "hold_return_within_baseline", 0.1))
        hold_s = float(getattr(cfg, "hold_grasp_rise_s", 1.0))
        gyro_max = float(getattr(cfg, "hold_gyro_max_dps", 90.0))
        moving = gyro_dps is not None and gyro_dps == gyro_dps and float(gyro_dps) > gyro_max
        sample = float(cover)

        if self.state == self.FREE:
            self._prune(now)
            # No settled samples (startup, or a gap longer than the window): this
            # frame is the baseline, not a rise against zero.
            if not self.cover_history:
                self._pending.clear()
                self.above_since = None
                self.peak_cover = 0.0
                self.cover_history.append((now, sample))
                self.baseline = sample
                return None
            self.baseline = self._rolling_baseline()
            elevated = (not moving) and sample >= self.baseline + rise
            if elevated:
                if self.above_since is None:
                    self.above_since = now
                    self.above_unix_ms = unix_ms
                    self.peak_cover = sample
                else:
                    self.peak_cover = max(self.peak_cover, sample)
                self._pending.append((now, sample))
                if now - self.above_since >= hold_s:
                    self.state = self.HOLDING
                    self.grasp_at = self.above_since
                    self.grasp_unix_ms = self.above_unix_ms
                    self.hold_started = self.above_since
                    self.baseline_at_grasp = self.baseline
                    self._pending.clear()
                    self.above_since = None
                return None
            self._cancel_candidate(now)
            self.cover_history.append((now, sample))
            self._prune(now)
            self.baseline = self._rolling_baseline()
            return None

        # HOLDING — release when cover returns near the frozen baseline.
        self.peak_cover = max(self.peak_cover, cover)
        if cover <= self.baseline_at_grasp + back:
            hold_ms = 0.0 if self.hold_started is None else (now - self.hold_started) * 1000.0
            event = self._emit(now, unix_ms, hold_ms)
            self.state = self.FREE
            self.above_since = None
            self.grasp_at = None
            self.hold_started = None
            # Seed history with the post-release cover so baseline recovers.
            self.cover_history.append((now, float(cover)))
            self._prune(now)
            return event
        return None

    def _emit(self, now: float, unix_ms: int, hold_ms: float) -> HoldEvent | None:
        cooldown = float(getattr(self.config, "hold_cooldown_s", 5.0))
        if self.last_event_at is not None and now - self.last_event_at < cooldown:
            self.peak_cover = 0.0
            return None
        self.last_event_at = now
        event = HoldEvent(
            at=now,
            unix_ms=unix_ms,
            source="hold",
            grasp_unix_ms=self.grasp_unix_ms,
            hold_ms=round(hold_ms, 1),
            peak_cover=round(self.peak_cover, 3),
            baseline=round(self.baseline_at_grasp, 3),
        )
        self.peak_cover = 0.0
        return event

    def mark(self, now: float, unix_ms: int) -> HoldEvent:
        """Ground-truth mark from the dashboard (P)."""
        self.last_event_at = now
        self.state = self.FREE
        self.above_since = None
        self.grasp_at = None
        self.hold_started = None
        self.peak_cover = 0.0
        self._pending.clear()
        return HoldEvent(
            at=now,
            unix_ms=unix_ms,
            source="marked",
            grasp_unix_ms=unix_ms,
            hold_ms=0.0,
            peak_cover=0.0,
            baseline=round(self.baseline, 3),
        )
