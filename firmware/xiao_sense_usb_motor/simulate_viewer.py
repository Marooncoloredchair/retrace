"""Run view_xiao_motor.py against a simulated board. No serial port is opened.

    ..\\..\\.venv\\Scripts\\python.exe simulate_viewer.py

This checks the viewer only: frame decoding, the V button, the per-pulse
counts, restart detection and the lost-connection path. It says nothing about
the real firmware or the real hardware.

The fake board sends a JPEG and an IMU sample about every 36 ms and a status
line every 2 seconds. It answers 'V' with the same two motor messages the
firmware sends. At 5 seconds it pretends to restart after a brownout, and at
7 seconds the port raises the error Windows gives when the device disappears.
"""

from __future__ import annotations

import struct
import sys
import time
import tkinter as tk

import cv2
import numpy as np
import serial

import framing
import view_xiao_motor as viewer


def frame(kind: int, payload: bytes) -> bytes:
    return framing.HEADER.pack(framing.MAGIC, kind, 0, len(payload)) + payload + struct.pack(
        "<I", framing.crc32_ieee(payload)
    )


class FakeBoard:
    def __init__(self) -> None:
        image = np.zeros((240, 320, 3), dtype=np.uint8)
        image[:, :, 1] = np.linspace(0, 255, 320, dtype=np.uint8)
        self.jpeg = cv2.imencode(".jpg", image)[1].tobytes()
        self.out = bytearray()
        self.started = time.monotonic()
        self.boot_at = self.started
        self.boot = 1
        self.reset = "POWERON"
        self.next_frame = self.started
        self.next_status = self.started
        self.pulses = 0
        self.pulse_end = None
        self.restarted = False
        self.written = bytearray()

    def _text(self, text: str) -> None:
        self.out += frame(framing.FRAME_TEXT, text.encode())

    def _tick(self) -> None:
        now = time.monotonic()
        if now - self.started > 7:
            raise serial.SerialException("simulated: device disappeared")
        if not self.restarted and now - self.started > 5:
            self.restarted = True
            self.boot_at = now
            self.boot = 2
            self.reset = "BROWNOUT"
            self.next_status = now
        up_ms = int((now - self.boot_at) * 1000)
        if self.pulse_end is not None and now >= self.pulse_end:
            self.pulse_end = None
            self._text(f"motor pulse {self.pulses} end on_us 200011 d3 LOW")
        if now >= self.next_status:
            self.next_status = now + 2
            self._text(
                f"status boot {self.boot} reset {self.reset} up_ms {up_ms} motor idle "
                f"pulses {self.pulses} ignored 0 d3 LOW"
            )
        if now >= self.next_frame:
            self.next_frame = now + 0.036
            self.out += frame(framing.FRAME_IMU, framing.IMU_PACKET.pack(0x68, 0x68, 0, 0, up_ms, 0, 0, 16384, 0, 0, 0, 0))
            self.out += frame(framing.FRAME_JPEG, self.jpeg)

    def read(self, count: int) -> bytes:
        self._tick()
        if not self.out:
            time.sleep(0.005)
            return b""
        data = bytes(self.out[:count])
        del self.out[:count]
        return data

    def write(self, data: bytes) -> int:
        self.written += data
        if data == b"V":
            self.pulses += 1
            self._text(f"motor pulse {self.pulses} start 200 ms")
            self.pulse_end = time.monotonic() + 0.2
        return len(data)

    def close(self) -> None:
        pass


class AutoTk(tk.Tk):
    """Presses the Vibrate button by itself at 1.5 s."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.after(1500, self._press)

    def _press(self) -> None:
        for child in self.winfo_children():
            if isinstance(child, tk.Button):
                child.invoke()


def main() -> int:
    board = FakeBoard()
    viewer.open_port = lambda name: board
    viewer.tk.Tk = AutoTk
    sys.argv = ["view_xiao_motor.py", "--port", "SIMULATED"]
    code = viewer.main()
    print(f"SIMULATION ONLY. viewer exit code {code}. bytes the viewer wrote to the fake port: {bytes(board.written)!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
