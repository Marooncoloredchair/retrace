"""Show real JPEG frames from the XIAO ESP32S3 Sense USB stream.

The window only updates when a framed JPEG arrives. Press Q or close the
window to stop. Nothing is drawn before the first real frame.
"""

from __future__ import annotations

import argparse
import sys
import time

import tkinter as tk

import cv2
import numpy as np
import serial

from framing import FRAME_IMU, FRAME_TEXT, FrameReader, decode_imu, open_port


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM5")
    args = parser.parse_args()

    print(f"Opening {args.port}. Waiting for a real JPEG. Press Q in the image window to stop.", flush=True)
    try:
        port = open_port(args.port)
    except serial.SerialException as error:
        print(f"Could not open {args.port}: {error}", file=sys.stderr)
        return 1

    reader = FrameReader(port)
    shown = 0
    invalid = 0
    text_count = 0
    imu_count = 0
    imu_bad = 0
    started = time.monotonic()
    last_report = started
    last_imu_print = started
    fps = 0.0
    imu_rate = 0.0
    span = {
        "ax_g": [None, None],
        "ay_g": [None, None],
        "az_g": [None, None],
        "gx_dps": [None, None],
        "gy_dps": [None, None],
        "gz_dps": [None, None],
    }
    root = tk.Tk()
    root.title("XIAO ESP32S3 Sense")
    status = tk.StringVar(value="Waiting for a real JPEG")
    imu_status = tk.StringVar(value="Waiting for an IMU sample")
    imu_range = tk.StringVar(value="Move the sensor to see the live range")
    tk.Label(root, textvariable=status).pack()
    tk.Label(root, textvariable=imu_status, font=("Consolas", 12), justify=tk.LEFT).pack(anchor="w")
    tk.Label(root, textvariable=imu_range, font=("Consolas", 11), justify=tk.LEFT).pack(anchor="w")
    panel = tk.Label(root)
    panel.pack()
    closed = {"value": False}

    def close(_event=None) -> None:
        closed["value"] = True
        root.quit()

    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<KeyPress-q>", close)
    root.bind("<KeyPress-Q>", close)
    try:
        while not closed["value"]:
            frame = reader.next_frame()
            root.update_idletasks()
            root.update()
            if frame is None:
                continue
            kind, payload = frame
            if kind == FRAME_TEXT:
                text_count += 1
                print(payload.decode("utf-8", errors="replace"), flush=True)
                continue
            if kind == FRAME_IMU:
                sample = decode_imu(payload)
                if sample is None or not sample["scaled"]:
                    imu_bad += 1
                    continue
                imu_count += 1
                for key in span:
                    value = sample[key]
                    low, high = span[key]
                    span[key][0] = value if low is None else min(low, value)
                    span[key][1] = value if high is None else max(high, value)
                elapsed_imu = time.monotonic() - started
                if elapsed_imu > 0:
                    imu_rate = imu_count / elapsed_imu
                imu_status.set(
                    f"accel g  X {sample['ax_g']:+7.3f}  Y {sample['ay_g']:+7.3f}  Z {sample['az_g']:+7.3f}\n"
                    f"gyro dps X {sample['gx_dps']:+7.1f}  Y {sample['gy_dps']:+7.1f}  Z {sample['gz_dps']:+7.1f}\n"
                    f"raw ax {sample['ax_raw']:6d} ay {sample['ay_raw']:6d} az {sample['az_raw']:6d}  "
                    f"gx {sample['gx_raw']:6d} gy {sample['gy_raw']:6d} gz {sample['gz_raw']:6d}\n"
                    f"addr 0x{sample['address']:02x} whoami 0x{sample['whoami']:02x}  "
                    f"{imu_rate:.1f} imu samples/s  chip {sample['temp_c']:.1f} C"
                )
                imu_range.set(
                    "range g  "
                    f"X {span['ax_g'][0]:+.3f}..{span['ax_g'][1]:+.3f}  "
                    f"Y {span['ay_g'][0]:+.3f}..{span['ay_g'][1]:+.3f}  "
                    f"Z {span['az_g'][0]:+.3f}..{span['az_g'][1]:+.3f}\n"
                    "range dps  "
                    f"X {span['gx_dps'][0]:+.1f}..{span['gx_dps'][1]:+.1f}  "
                    f"Y {span['gy_dps'][0]:+.1f}..{span['gy_dps'][1]:+.1f}  "
                    f"Z {span['gz_dps'][0]:+.1f}..{span['gz_dps'][1]:+.1f}"
                )
                now_imu = time.monotonic()
                if now_imu - last_imu_print >= 0.5:
                    print(
                        f"imu {sample['ax_g']:+.3f} {sample['ay_g']:+.3f} {sample['az_g']:+.3f} g  "
                        f"{sample['gx_dps']:+.1f} {sample['gy_dps']:+.1f} {sample['gz_dps']:+.1f} dps  "
                        f"raw {sample['ax_raw']} {sample['ay_raw']} {sample['az_raw']} "
                        f"{sample['gx_raw']} {sample['gy_raw']} {sample['gz_raw']}",
                        flush=True,
                    )
                    last_imu_print = now_imu
                continue
            if len(payload) < 4 or payload[0:2] != b"\xff\xd8":
                invalid += 1
                continue
            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                invalid += 1
                continue
            shown += 1
            now = time.monotonic()
            elapsed = now - started
            if elapsed > 0:
                fps = shown / elapsed
            height, width = image.shape[:2]
            label = f"{width}x{height}  {fps:.1f} fps  frames {shown}  invalid {invalid}"
            status.set(label + "   Press Q or close the window")
            ok, encoded = cv2.imencode(".png", image)
            if not ok:
                invalid += 1
                continue
            photo = tk.PhotoImage(data=encoded.tobytes())
            panel.configure(image=photo)
            panel.image = photo
            if now - last_report >= 2:
                print(label, flush=True)
                last_report = now
    finally:
        port.close()
        root.destroy()
    print(
        f"Stopped. Real frames shown: {shown}. Invalid frames: {invalid}. "
        f"Text messages: {text_count}. IMU samples: {imu_count}. Bad IMU frames: {imu_bad}.",
        flush=True,
    )
    return 0 if shown else 2


if __name__ == "__main__":
    raise SystemExit(main())
