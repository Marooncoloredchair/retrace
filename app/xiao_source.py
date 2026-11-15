"""Feed the XIAO ESP32S3 Sense USB camera into the existing frame hub.

The standalone viewer and this source both need the same serial port. This
source opens the port only while XIAO USB is the selected camera, and it
closes the port when the selection returns to the iPhone.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections import deque
from pathlib import Path

import serial
from serial.tools import list_ports

ROOT = Path(__file__).resolve().parents[1]
FRAMING_PATH = ROOT / "firmware" / "xiao_sense_usb" / "framing.py"
MAX_PROCESS_FPS = 10
ESP_VID = 0x303A
ESP_PID = 0x1001


def _framing():
    spec = importlib.util.spec_from_file_location("xiao_framing", FRAMING_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("The XIAO frame reader is missing.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_xiao_port() -> str:
    """Return the one Espressif USB serial port. Do not guess another COM port."""
    matches = []
    for port in list_ports.comports():
        if port.vid == ESP_VID and port.pid == ESP_PID and port.device:
            matches.append(port.device)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise RuntimeError("The XIAO is not on a USB serial port. Connect it with the data cable.")
    raise RuntimeError("More than one Espressif serial port is connected. Unplug the extra board.")


class XiaoSource:
    """Publish at most 10 camera frames per second into the hub."""

    def __init__(self, hub) -> None:
        self.hub = hub
        self.task: asyncio.Task | None = None
        self.port_name = ""
        self.connected = False
        self.error = ""
        self.sensor = ""
        self.dropped = 0
        self.invalid = 0
        self.published = 0
        self.arrival_times: deque[float] = deque(maxlen=120)
        self._stop = asyncio.Event()

    def _counts(self) -> dict:
        now = time.monotonic()
        while self.arrival_times and now - self.arrival_times[0] > 1:
            self.arrival_times.popleft()
        return {
            "camera_source": self.hub.camera_source,
            "xiao_connected": self.connected,
            "xiao_port": self.port_name,
            "camera_fps": len(self.arrival_times),
            "process_limit_fps": MAX_PROCESS_FPS,
            "frames_dropped": self.dropped,
            "frames_invalid": self.invalid,
            "frames_published": self.published,
            "xiao_error": self.error,
            "sensor": self.sensor,
        }

    async def status(self) -> dict:
        return self._counts()

    async def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self._stop = asyncio.Event()
        self.error = ""
        self.task = asyncio.create_task(self._run())
        for _ in range(20):
            await asyncio.sleep(0.1)
            if self.connected or self.error:
                break
        if self.error:
            raise RuntimeError(self.error)

    async def stop(self) -> None:
        self._stop.set()
        task = self.task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=2)
            except TimeoutError:
                task.cancel()
        self.connected = False
        self.task = None

    async def _run(self) -> None:
        framing = _framing()
        port = None
        try:
            self.port_name = await asyncio.to_thread(find_xiao_port)
            try:
                port = await asyncio.to_thread(framing.open_port, self.port_name)
            except serial.SerialException as error:
                self.error = (
                    f"{self.port_name} is busy. Close the XIAO camera window, then select XIAO USB again. {error}"
                )
                return
            self.connected = True
            reader = framing.FrameReader(port)
            next_publish = 0.0
            sequence = 0
            while not self._stop.is_set():
                frame = await asyncio.to_thread(reader.next_frame)
                if frame is None:
                    continue
                kind, payload = frame
                if kind == framing.FRAME_TEXT:
                    self.sensor = payload.decode("utf-8", errors="replace")
                    continue
                if kind != framing.FRAME_JPEG:
                    continue
                if len(payload) < 4 or payload[0:2] != b"\xff\xd8":
                    self.invalid += 1
                    continue
                now = time.monotonic()
                self.arrival_times.append(now)
                if now < next_publish:
                    self.dropped += 1
                    continue
                next_publish = now + (1.0 / MAX_PROCESS_FPS)
                sequence = (sequence + 1) & 0xFFFFFFFF
                unix_ms = int(time.time() * 1000)
                packet = sequence.to_bytes(4, "big") + unix_ms.to_bytes(8, "big") + payload
                await self.hub.publish(packet, source="xiao_usb")
                self.published += 1
        except RuntimeError as error:
            self.error = str(error)
        except Exception as error:
            self.error = f"XIAO camera stopped. {error}"
        finally:
            self.connected = False
            if port is not None:
                await asyncio.to_thread(port.close)
