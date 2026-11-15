"""Listen to the combined firmware and report what arrived. No window.

    ..\\..\\.venv\\Scripts\\python.exe listen_xiao.py --seconds 60

READ-ONLY. This script never writes to the serial port, so it cannot trigger
the motor. It opens the port with DTR and RTS off, like the viewer.

It counts JPEG frames (each one is decoded to check it is a real image), IMU
samples and text frames, and it watches for a board restart, a brownout reset
reason, a lost USB connection, and bytes that were not part of a valid frame.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import time

import cv2
import numpy as np
import serial
from serial.tools import list_ports

import framing


class CountingPort:
    """Pass reads through and count the bytes. There is no write method."""

    def __init__(self, port: serial.Serial) -> None:
        self._port = port
        self.bytes_read = 0

    def read(self, count: int) -> bytes:
        data = self._port.read(count)
        self.bytes_read += len(data)
        return data


def find_port() -> str:
    matches = [p.device for p in list_ports.comports() if p.vid == 0x303A and p.pid == 0x1001]
    if len(matches) != 1:
        raise SystemExit(f"Expected one Espressif 303A:1001 port, found {matches}. Nothing opened.")
    return matches[0]


def parse_status(text: str) -> dict | None:
    tokens = text.split()
    if not tokens or tokens[0] != "status" or len(tokens) % 2 == 0:
        return None
    return dict(zip(tokens[1::2], tokens[2::2]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--save-frame", default="", help="write one received JPEG to this path")
    args = parser.parse_args()

    name = args.port or find_port()
    print(f"Opening {name} read-only (DTR off, RTS off) for {args.seconds:.0f} s. Nothing is written.", flush=True)
    raw_port = framing.open_port(name)
    port = CountingPort(raw_port)
    reader = framing.FrameReader(port)

    started = time.monotonic()
    framed_bytes = 0
    jpeg_times: list[float] = []
    jpeg_sizes: list[int] = []
    jpeg_dims: dict[tuple[int, int], int] = {}
    jpeg_bad = 0
    jpeg_mean_levels: list[float] = []
    saved = False
    imu_times: list[float] = []
    imu_ms: list[int] = []
    imu_ids: set[tuple[int, int]] = set()
    imu_bad = 0
    accel_mag: list[float] = []
    gyro_abs_max = 0.0
    temps: list[float] = []
    texts: list[tuple[float, str]] = []
    statuses: list[tuple[float, dict]] = []
    restarts: list[str] = []
    lost = ""

    try:
        while time.monotonic() - started < args.seconds:
            try:
                frame = reader.next_frame()
            except serial.SerialException as error:
                lost = f"at {time.monotonic() - started:.1f} s: {error}"
                break
            if frame is None:
                continue
            now = time.monotonic() - started
            kind, payload = frame
            framed_bytes += framing.HEADER.size + len(payload) + 4
            if kind == framing.FRAME_TEXT:
                text = payload.decode("utf-8", errors="replace")
                texts.append((now, text))
                state = parse_status(text)
                if state is not None and state.get("up_ms", "").isdigit():
                    if statuses:
                        previous = statuses[-1][1]
                        if int(state["up_ms"]) < int(previous["up_ms"]) or state.get("boot") != previous.get("boot"):
                            restarts.append(f"at {now:.1f} s: {text}")
                    statuses.append((now, state))
            elif kind == framing.FRAME_IMU:
                sample = framing.decode_imu(payload)
                if sample is None or not sample["scaled"]:
                    imu_bad += 1
                    continue
                if imu_ms and sample["sample_ms"] < imu_ms[-1]:
                    restarts.append(f"at {now:.1f} s: IMU clock went back from {imu_ms[-1]} to {sample['sample_ms']} ms")
                imu_times.append(now)
                imu_ms.append(sample["sample_ms"])
                imu_ids.add((sample["address"], sample["whoami"]))
                accel_mag.append(math.sqrt(sample["ax_g"] ** 2 + sample["ay_g"] ** 2 + sample["az_g"] ** 2))
                gyro_abs_max = max(gyro_abs_max, abs(sample["gx_dps"]), abs(sample["gy_dps"]), abs(sample["gz_dps"]))
                temps.append(sample["temp_c"])
            else:
                image = None
                if len(payload) >= 4 and payload[0:2] == b"\xff\xd8":
                    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    jpeg_bad += 1
                    continue
                jpeg_times.append(now)
                jpeg_sizes.append(len(payload))
                height, width = image.shape[:2]
                jpeg_dims[(width, height)] = jpeg_dims.get((width, height), 0) + 1
                if len(jpeg_times) % 15 == 1:
                    jpeg_mean_levels.append(float(image.mean()))
                if args.save_frame and not saved and now >= 3.0:
                    with open(args.save_frame, "wb") as handle:
                        handle.write(payload)
                    saved = True
    finally:
        try:
            raw_port.close()
        except serial.SerialException:
            pass

    elapsed = time.monotonic() - started
    unframed = port.bytes_read - framed_bytes - len(reader.buffer)
    print(f"Closed {name} after {elapsed:.1f} s. Bytes read {port.bytes_read}.")
    print(f"Bytes outside valid frames (CRC or sync failures): {unframed}. Left in buffer at stop: {len(reader.buffer)}.")

    print("\n== Text frames ==")
    print(f"count {len(texts)}")
    for at, text in texts[:14]:
        print(f"  {at:6.2f} s  {text}")
    shapes: dict[str, int] = {}
    for _, text in texts:
        shape = re.sub(r"\d+", "N", text)
        shapes[shape] = shapes.get(shape, 0) + 1
    print("line shapes (digits shown as N):")
    for shape, count in shapes.items():
        print(f"  {count:4d}  {shape}")
    if statuses:
        print(f"first status: {' '.join(f'{k} {v}' for k, v in statuses[0][1].items())}")
        print(f"last status:  {' '.join(f'{k} {v}' for k, v in statuses[-1][1].items())}")
        print(f"reset reasons seen: {sorted({s.get('reset', '?') for _, s in statuses})}")
        print(f"boot numbers seen: {sorted({s.get('boot', '?') for _, s in statuses})}")
        print(f"d3 states seen: {sorted({s.get('d3', '?') for _, s in statuses})}, "
              f"motor states seen: {sorted({s.get('motor', '?') for _, s in statuses})}, "
              f"pulses: {sorted({s.get('pulses', '?') for _, s in statuses})}")
    else:
        print("NO status line arrived.")

    print("\n== Camera ==")
    print(f"valid JPEG frames {len(jpeg_times)}, invalid {jpeg_bad}, sizes {dict(jpeg_dims)}")
    if len(jpeg_times) >= 2:
        span = jpeg_times[-1] - jpeg_times[0]
        gaps = [b - a for a, b in zip(jpeg_times, jpeg_times[1:])]
        print(f"first frame at {jpeg_times[0]:.2f} s, last at {jpeg_times[-1]:.2f} s, "
              f"{(len(jpeg_times) - 1) / span:.1f} fps over {span:.1f} s")
        print(f"gap between frames: median {sorted(gaps)[len(gaps) // 2] * 1000:.0f} ms, longest {max(gaps) * 1000:.0f} ms")
        print(f"JPEG bytes: min {min(jpeg_sizes)}, mean {sum(jpeg_sizes) / len(jpeg_sizes):.0f}, max {max(jpeg_sizes)}")
        print(f"mean pixel level of sampled frames: min {min(jpeg_mean_levels):.0f}, max {max(jpeg_mean_levels):.0f} (0 black, 255 white)")
    if args.save_frame:
        print(f"saved one frame to {args.save_frame}: {'yes' if saved else 'NO'}")

    print("\n== IMU ==")
    print(f"valid samples {len(imu_times)}, bad {imu_bad}, (address, whoami) seen: "
          f"{sorted((hex(a), hex(w)) for a, w in imu_ids)}")
    if len(imu_times) >= 2:
        span = imu_times[-1] - imu_times[0]
        steps = [b - a for a, b in zip(imu_ms, imu_ms[1:])]
        print(f"{(len(imu_times) - 1) / span:.1f} samples/s over {span:.1f} s")
        print(f"board clock between samples: median {sorted(steps)[len(steps) // 2]} ms, longest {max(steps)} ms")
        print(f"acceleration magnitude: min {min(accel_mag):.3f} g, mean {sum(accel_mag) / len(accel_mag):.3f} g, max {max(accel_mag):.3f} g")
        print(f"largest gyro reading {gyro_abs_max:.1f} dps, chip temperature {min(temps):.1f} to {max(temps):.1f} C")

    print("\n== Stability ==")
    print(f"board restarts seen while listening: {len(restarts)}")
    for line in restarts:
        print(f"  {line}")
    print(f"USB connection lost: {lost or 'no'}")
    return 3 if lost else 0


if __name__ == "__main__":
    sys.exit(main())
