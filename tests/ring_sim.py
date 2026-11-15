"""Stand-ins for the ring, for tests only. Nothing here opens a serial port.

World       a long drawn wall. view(x) is the 320x240 picture a camera sees at x.
            Moving along x plays the part of walking; it is not a model of a room.
SimRing     a fake board that speaks the real framing: JPEG, IMU and status
            frames, and the firmware's V rules (200 ms pulse, 800 ms rest,
            commands in between are counted as ignored). It can be unplugged.
Rig         an Engine with a fake clock and a link that never touches hardware,
            so a whole save / walk / find story runs in a second.
"""

from __future__ import annotations

import shutil
import struct
import tempfile
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import serial

from ring.config import Config
from ring.engine import Engine
from ring.link import framing
from ring.store import Store

STEP_PX = 80  # one "step" along the wall between kept pictures


def encode(image: np.ndarray, quality: int = 85) -> bytes:
    return cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])[1].tobytes()


class World:
    def __init__(self, seed: int, steps: int = 30) -> None:
        self.width = 320 + STEP_PX * (steps + 4)
        rng = np.random.default_rng(seed)
        wall = rng.integers(90, 170, (240, self.width, 3), dtype=np.uint8)
        wall = cv2.GaussianBlur(wall, (0, 0), 3)
        for index in range(self.width // 9):
            x, y = int(rng.integers(0, self.width)), int(rng.integers(0, 240))
            w, h = int(rng.integers(8, 60)), int(rng.integers(8, 50))
            color = tuple(int(value) for value in rng.integers(0, 255, 3))
            choice = rng.random()
            if choice < 0.4:
                cv2.rectangle(wall, (x, y), (x + w, y + h), color, -1 if rng.random() < 0.6 else 2)
            elif choice < 0.7:
                cv2.circle(wall, (x, y), max(4, w // 3), color, -1 if rng.random() < 0.6 else 2)
            else:
                cv2.putText(wall, f"{seed}{index:03d}", (x, max(14, y)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        self.wall = wall
        self.rng = np.random.default_rng(seed + 1000)

    def repeat(self, source_step: int, target_step: int, steps: int = 5) -> None:
        """Make one stretch of wall an exact copy of another: two places that look the same."""
        a, b = source_step * STEP_PX, target_step * STEP_PX
        span = 320 + steps * STEP_PX
        self.wall[:, b : b + span] = self.wall[:, a : a + span]

    def view(self, step: float, jitter: int = 3, noise: float = 2.0) -> np.ndarray:
        x = int(round(step * STEP_PX))
        dx = int(self.rng.integers(-jitter, jitter + 1)) if jitter else 0
        x = min(max(0, x + dx), self.width - 320)
        picture = self.wall[:, x : x + 320].astype(np.int16)
        if noise:
            picture += self.rng.normal(0, noise, picture.shape).astype(np.int16)
        return np.clip(picture, 0, 255).astype(np.uint8)


def blurred(image: np.ndarray, sigma: float = 6.0) -> np.ndarray:
    return cv2.GaussianBlur(image, (0, 0), sigma)


def hazy(image: np.ndarray) -> np.ndarray:
    """An open lens on a soft view: large light and dark areas, no fine detail."""
    picture = blurred(image).astype(np.float32)
    ramp = np.linspace(0.3, 1.7, picture.shape[1], dtype=np.float32)[None, :, None]
    return np.clip(picture * ramp, 0, 255).astype(np.uint8)


def covered(image: np.ndarray, share: float = 0.8) -> np.ndarray:
    """A finger over most of the lens: a flat skin-coloured area."""
    picture = image.copy()
    picture[:, : int(picture.shape[1] * share)] = (120, 140, 190)
    return picture


def imu_sample(gyro_dps: float = 2.0, board_ms: int = 0) -> dict:
    return {
        "address": 0x68, "whoami": 0x68, "sample_ms": board_ms, "scaled": True, "accel_fs": 0, "gyro_fs": 0,
        "ax_raw": 0, "ay_raw": 0, "az_raw": 16384, "temp_raw": 0, "gx_raw": 0, "gy_raw": 0, "gz_raw": 0,
        "ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0, "gx_dps": gyro_dps, "gy_dps": 0.0, "gz_dps": 0.0, "temp_c": 30.0,
        "host_mono": 0.0, "host_unix": 0.0,
    }


class StubLink:
    """What the Engine needs from a link, with no thread and no port."""

    def __init__(self) -> None:
        self.connected = True
        self.pulses = 0
        self.events: list[dict] = []
        self.transport = "serial"
        self.camera_fps = 28.0
        self.imu_count = 0
        self.imu_dropped = 0
        self.imu_rate_hz = 28.0
        self.outbox = bytearray()

    def send_bytes(self, data: bytes) -> None:
        self.outbox += bytes(data)

    def request_pulse(self) -> bool:
        if not self.connected:
            return False
        self.pulses += 1
        return True

    def drain_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    def status(self) -> dict:
        return {
            "connected": self.connected, "transport": self.transport, "port": "SIM", "error": "",
            "connects": 1, "disconnects": 0,
            "camera_fps": self.camera_fps, "frames_valid": 0, "frames_invalid": 0,
            "imu": {
                "ok": True, "rate_hz": self.imu_rate_hz, "samples": self.imu_count, "bad": 0,
                "dropped": self.imu_dropped, "age_s": 0.0, "last": None, "line": "",
            },
            "board": {}, "board_restarts": 0, "sensor": "", "writes": self.pulses, "writes_allowed": True,
            "board_pulse_starts": self.pulses, "board_pulse_ends": self.pulses, "board_ignored": 0,
            "last_pulse_on_us": None,
        }


class Rig:
    """An Engine driven frame by frame with a clock the test controls."""

    def __init__(self, folder: Path | None = None, **settings) -> None:
        self.owns_folder = folder is None
        self.folder = Path(tempfile.mkdtemp(prefix="retrace-test-")) if folder is None else folder
        self.now = 1000.0
        self.wall0 = 1_800_000_000.0
        self.seq = 0
        settings.setdefault("embedder_enabled", False)  # tests do not need DINOv2 / torch
        self.config = Config(**settings)
        self.link = StubLink()
        self.store = Store(self.folder)
        self.engine = Engine(self.config, self.store, self.link, clock=lambda: self.now, wall=self.wall)
        self.outcomes: list[dict] = []  # every vibration request and what became of it
        request = self.engine.motor.request

        def recording(reason: str, detail: str = "") -> dict:
            outcome = request(reason, detail)
            self.outcomes.append(outcome)
            return outcome

        self.engine.motor.request = recording

    def wall(self) -> float:
        return self.wall0 + (self.now - 1000.0)

    def close(self, delete: bool | None = None) -> None:
        self.engine.stop()
        self.store.close()
        if self.owns_folder if delete is None else delete:
            shutil.rmtree(self.folder, ignore_errors=True)

    def wait(self, seconds: float) -> dict:
        self.now += seconds
        self.engine.tick()
        return self.engine.publish()

    def show(self, image: np.ndarray | bytes, dt: float = 0.5, gyro_dps: float = 2.0) -> dict:
        """Advance the clock, give the engine one camera frame, and return the dashboard state."""
        self.now += dt
        self.seq += 1
        jpeg = image if isinstance(image, (bytes, bytearray)) else encode(image)
        sample = imu_sample(gyro_dps, int(self.now * 1000) & 0xFFFFFFFF)
        sample["host_unix"] = self.wall()
        self.engine.process(
            {"seq": self.seq, "jpeg": bytes(jpeg), "host_mono": self.now, "host_unix": self.wall(), "imu": sample, "imu_samples": [sample]}
        )
        self.engine.tick()
        return self.engine.publish()

    # A whole story, used by several tests -------------------------------------------

    def save_at_spot(self, world: World, name: str = "Keys") -> int:
        """Look around the spot (steps 0 to 2), press Save, keep looking until the save finishes."""
        for step in (0.0, 1.0):
            self.show(world.view(step))
        item_id = self.engine.save_item(name)["item_id"]
        for step in (2.0, 0.5):
            self.show(world.view(step))
        self.wait(self.config.save_capture_s + 0.1)
        return item_id

    def walk_away(self, world: World, steps: int, start: float = 3.0) -> None:
        """Breadcrumb i is the view at step 2 + i."""
        for index in range(steps):
            self.show(world.view(start + index))

    def find_state(self) -> dict:
        return self.engine.publish()["find"]

    def sent(self) -> list[dict]:
        return [entry for entry in self.outcomes if entry["sent"]]


def frame_bytes(kind: int, payload: bytes) -> bytes:
    return framing.HEADER.pack(framing.MAGIC, kind, 0, len(payload)) + payload + struct.pack(
        "<I", framing.crc32_ieee(payload)
    )


class SimPort:
    def __init__(self, ring: "SimRing") -> None:
        self.ring = ring
        self.out = bytearray()
        self.closed = False
        self.boot_at = time.monotonic()
        self.next_frame = self.boot_at
        self.next_status = self.boot_at
        self.pulses = 0
        self.ignored = 0
        self.pulse_end = None
        self.rest_until = 0.0
        self._text("sensor OV3660 QVGA jpeg (simulated)")
        self._text("imu MPU6050 0x68 whoami 0x68 (simulated)")
        self.out += bytes(ring.junk)

    def _text(self, text: str) -> None:
        self.out += frame_bytes(framing.FRAME_TEXT, text.encode())

    def _tick(self) -> None:
        ring = self.ring
        if not ring.plugged or self.closed:
            raise serial.SerialException("simulated: device disappeared")
        now = time.monotonic()
        up_ms = int((now - self.boot_at) * 1000)
        if self.pulse_end is not None and now >= self.pulse_end:
            self.pulse_end = None
            self.rest_until = now + getattr(self, "rest_s", 0.8)
            self._text(f"motor pulse {self.pulses} end on_us {getattr(self, 'pulse_on_us', 200011)} d3 LOW")
        if now >= self.next_status:
            self.next_status = now + 2.0
            state = "idle" if self.pulse_end is None else "on"
            self._text(
                f"status boot {ring.boot} reset POWERON up_ms {up_ms} motor {state} pulses {self.pulses} ignored {self.ignored} d3 LOW"
            )
        if ring.silent:
            return
        if now >= self.next_frame:
            self.next_frame = now + ring.frame_period
            gyro = int(ring.gyro_dps * 32768 / 250)
            self.out += frame_bytes(
                framing.FRAME_IMU, framing.IMU_PACKET.pack(0x68, 0x68, 0, 0, up_ms & 0xFFFFFFFF, 0, 0, 16384, 0, gyro, 0, 0)
            )
            jpeg = ring.jpeg()
            if ring.corrupt_next:
                ring.corrupt_next -= 1
                broken = bytearray(frame_bytes(framing.FRAME_JPEG, jpeg))
                broken[-1] ^= 0xFF
                self.out += broken
            else:
                self.out += frame_bytes(framing.FRAME_JPEG, jpeg)

    def read(self, count: int) -> bytes:
        self._tick()
        if not self.out:
            time.sleep(0.004)
            return b""
        data = bytes(self.out[:count])
        del self.out[:count]
        return data

    def write(self, data: bytes) -> int:
        if not self.ring.plugged or self.closed:
            raise serial.SerialException("simulated: device disappeared")
        with self.ring.lock:
            self.ring.written += data
        for byte in data:
            if byte not in (ord("V"), ord("v")):
                continue
            tick = byte == ord("v")
            now = time.monotonic()
            if self.pulse_end is not None or now < self.rest_until:
                self.ignored += 1
                self._text(f"motor command ignored, pulse active or resting, ignored {self.ignored}")
            else:
                self.pulses += 1
                self.ring.pulses_total += 1
                self.pulse_on_us = 80011 if tick else 200011
                self.rest_s = 0.22 if tick else 0.8
                self._text(f"motor pulse {self.pulses} start {80 if tick else 200} ms")
                self.pulse_end = now + (0.08 if tick else 0.2)
        return len(data)

    def close(self) -> None:
        self.closed = True


class SimRing:
    """A fake board. Pass ring.opener to RingLink."""

    def __init__(self, picture: np.ndarray, frame_period: float = 0.036) -> None:
        self.lock = threading.Lock()
        self.plugged = True
        self.silent = False
        self.boot = 0
        self.opens = 0
        self.written = bytearray()
        self.pulses_total = 0
        self.frame_period = frame_period
        self.gyro_dps = 2.0
        self.junk = b""
        self.corrupt_next = 0
        self.set_picture(picture)

    def set_picture(self, picture: np.ndarray) -> None:
        self._jpeg = encode(picture)

    def jpeg(self) -> bytes:
        return self._jpeg

    def unplug(self) -> None:
        self.plugged = False

    def plug(self) -> None:
        self.plugged = True

    def opener(self):
        if not self.plugged:
            raise RuntimeError("The XIAO is not on a USB serial port. Connect it with the data cable.")
        self.opens += 1
        self.boot += 1
        return SimPort(self), "SIM"


def wait_until(check, seconds: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return bool(check())
