"""In-memory latest-frame hub.

The phone publishes JPEG frames. Desktop viewers always receive the newest
frame only. Older frames are discarded and nothing is written to disk.

Binary frame layout, big-endian:
  bytes 0-3   uint32 sequence number
  bytes 4-11  uint64 capture time, Unix epoch milliseconds from the phone
  bytes 12+   JPEG (must start with FF D8)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from fastapi import WebSocket

log = logging.getLogger("retrace")

HEADER_BYTES = 12
MAX_FRAME_BYTES = 400_000


def valid_frame(payload: bytes) -> bool:
    """Return True when payload is a bounded header plus a JPEG."""
    if len(payload) < HEADER_BYTES + 32 or len(payload) > MAX_FRAME_BYTES:
        return False
    return payload[HEADER_BYTES] == 0xFF and payload[HEADER_BYTES + 1] == 0xD8


class Hub:
    """One live session. Holds a single frame in memory."""

    def __init__(self) -> None:
        self.cond = asyncio.Condition()
        self.latest: bytes | None = None
        self.version = 0
        self.frames_received = 0
        self.desktop_viewers = 0
        self.last_frame_at = 0.0
        self.receive_times: deque[float] = deque(maxlen=60)
        self.phones: set[WebSocket] = set()
        self.camera_source = "iphone"

    async def attach_phone(self, websocket: WebSocket) -> None:
        """Keep one phone publisher. A new phone replaces the previous one."""
        async with self.cond:
            previous = [phone for phone in self.phones if phone is not websocket]
            self.phones.add(websocket)
        for old in previous:
            await old.close(code=4000, reason="replaced")

    async def detach_phone(self, websocket: WebSocket) -> None:
        async with self.cond:
            self.phones.discard(websocket)

    async def change_desktop_viewers(self, delta: int) -> None:
        async with self.cond:
            self.desktop_viewers = max(0, self.desktop_viewers + delta)

    async def set_source(self, source: str) -> None:
        async with self.cond:
            self.camera_source = source

    async def publish(self, frame: bytes, source: str = "iphone") -> None:
        """Replace the stored frame. Waiters see this frame, not earlier ones."""
        first = False
        async with self.cond:
            if source != self.camera_source:
                return
            self.latest = frame
            self.version += 1
            self.frames_received += 1
            now = time.monotonic()
            self.last_frame_at = now
            self.receive_times.append(now)
            while self.receive_times and now - self.receive_times[0] > 1:
                self.receive_times.popleft()
            first = self.frames_received == 1
            self.cond.notify_all()
        if first:
            log.info("First camera frame received")

    async def next_frame(self, seen: int, timeout: float) -> tuple[bytes, int] | None:
        """Wait until a frame newer than `seen` exists, then return the latest."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self.cond:
            while self.version == seen or self.latest is None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self.cond.wait(), remaining)
                except TimeoutError:
                    if self.version == seen or self.latest is None:
                        return None
                    break
            assert self.latest is not None
            return self.latest, self.version

    async def status(self) -> dict:
        async with self.cond:
            now = time.monotonic()
            while self.receive_times and now - self.receive_times[0] > 1:
                self.receive_times.popleft()
            age = None
            if self.last_frame_at:
                age = int((now - self.last_frame_at) * 1000)
            return {
                "phone_connected": bool(self.phones),
                "camera_source": self.camera_source,
                "desktop_viewers": self.desktop_viewers,
                "frames_received": self.frames_received,
                "receive_fps": len(self.receive_times),
                "last_frame_age_ms": age,
            }
