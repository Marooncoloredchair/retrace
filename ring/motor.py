"""The only place that decides whether a vibration command is sent.

One command is the single byte V, which the tested firmware turns into one
200 ms pulse. There is one kind of pulse. A blocked request is dropped, never
queued, so the ring cannot buzz later for a reason that has passed.
"""

from __future__ import annotations

from collections import deque
from typing import Callable

REASONS = ("progress", "arrival", "manual test", "guide")


class MotorGovernor:
    def __init__(
        self,
        send: Callable[[], bool],
        clock: Callable[[], float],
        min_interval_s: float,
        max_per_minute: int,
        enabled: bool,
        locked: str = "",
        find_min_interval_s: float | None = None,
    ) -> None:
        self._send = send
        self._clock = clock
        self.locked = locked  # a reason nothing can be sent in this run, whatever the switch says
        self.min_interval_s = float(min_interval_s)
        self.max_per_minute = int(max_per_minute)
        self.enabled = bool(enabled)
        # Active Find uses this spacing and does not apply max_per_minute.
        # It is the firmware 'v' pulse plus rest (find_map_min_pulse_interval).
        self.find_min_interval_s = float(min_interval_s if find_min_interval_s is None else find_min_interval_s)
        self.finding = False
        self.sent_times: deque[float] = deque()
        self.requested = 0
        self.sent = 0
        self.blocked = 0
        self.log: deque[dict] = deque(maxlen=12)

    def request(self, reason: str, detail: str = "") -> dict:
        """Ask for one pulse. Returns what happened and why."""
        if reason not in REASONS:
            raise ValueError(f"Unknown vibration reason {reason!r}.")
        now = self._clock()
        self.requested += 1
        while self.sent_times and now - self.sent_times[0] >= 60.0:
            self.sent_times.popleft()
        interval = self.find_min_interval_s if self.finding else self.min_interval_s
        blocked = ""
        coalesced = False
        if self.locked:
            blocked = self.locked
        elif not self.enabled:
            blocked = "vibration is switched off"
        elif self.sent_times and interval - (now - self.sent_times[-1]) > 1e-4:
            # 1e-4 s absorbs the binary rounding of a 0.3 s tick. A tenth of a
            # millisecond is not a second pulse inside the firmware rest.
            if self.finding:
                # Another frame already asked inside this pulse. One send covers it.
                coalesced = True
            else:
                wait = interval - (now - self.sent_times[-1])
                blocked = f"too soon after the last pulse ({wait:.1f} s left)"
        elif not self.finding and len(self.sent_times) >= self.max_per_minute:
            blocked = f"limit of {self.max_per_minute} pulses per minute reached"
        elif not self._send():
            blocked = "the ring is not connected"
        sent = not blocked and not coalesced
        entry = {
            "at": now,
            "reason": reason,
            "detail": detail,
            "sent": sent,
            "blocked": blocked,
            "coalesced": coalesced,
        }
        if blocked:
            self.blocked += 1
        elif sent:
            self.sent += 1
            self.sent_times.append(now)
        if not coalesced:
            self.log.append(entry)
        return entry

    def public(self, wall_offset: float) -> dict:
        """wall_offset turns the monotonic times in the log into Unix time for display."""
        return {
            "enabled": self.enabled,
            "commands_sent": self.sent,
            "requests_blocked": self.blocked,
            "min_interval_s": self.min_interval_s,
            "max_per_minute": self.max_per_minute,
            "log": [
                {**entry, "unix_ms": int((entry["at"] + wall_offset) * 1000)} for entry in reversed(self.log)
            ],
        }
