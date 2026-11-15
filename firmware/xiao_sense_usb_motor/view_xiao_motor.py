"""Show real JPEG frames from the XIAO ESP32S3 Sense USB stream, and trigger the motor.

For the combined firmware in firmware/xiao_sense_usb_motor. The window only
updates when a framed JPEG arrives. Nothing is drawn before the first real frame.

Press V, or the button, to send the single byte 'V'. The firmware answers with
one 200 ms vibration pulse. Nothing is sent to the board at any other time.
After each V the viewer counts the frames and IMU samples that arrive in the
next 1.5 seconds and prints them, with the firmware's own motor messages.

Press Q or close the window to stop.
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

VIBRATE_COMMAND = b"V"
WINDOW_SECONDS = 1.5


def parse_status(text: str) -> dict | None:
    """Read 'status boot 1 reset POWERON up_ms 2345 ...' into a dict of strings."""
    tokens = text.split()
    if not tokens or tokens[0] != "status" or len(tokens) % 2 == 0:
        return None
    return dict(zip(tokens[1::2], tokens[2::2]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="COM5")
    args = parser.parse_args()

    print(
        f"Opening {args.port}. Waiting for a real JPEG. Press V for one vibration pulse, Q to stop.",
        flush=True,
    )
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
    board_status = tk.StringVar(value="Waiting for a status line from the board")
    motor_status = tk.StringVar(value="No vibration sent yet")
    tk.Label(root, textvariable=board_status, font=("Consolas", 11), justify=tk.LEFT).pack(anchor="w")
    tk.Label(root, textvariable=motor_status, font=("Consolas", 11), justify=tk.LEFT).pack(anchor="w")
    panel = tk.Label(root)
    panel.pack()
    closed = {"value": False}
    motor = {"window": None, "sent": 0}
    board = {"up_ms": None, "boot": None, "restarts": 0}
    lost = False

    def close(_event=None) -> None:
        closed["value"] = True
        root.quit()

    def vibrate(_event=None) -> None:
        if motor["window"] is not None:
            print("V not sent. The previous pulse is still being measured.", flush=True)
            return
        try:
            port.write(VIBRATE_COMMAND)
        except serial.SerialException as error:
            print(f"Could not send V: {error}", flush=True)
            return
        now = time.monotonic()
        motor["sent"] += 1
        motor["window"] = {
            "t0": now,
            "shown": shown,
            "imu": imu_count,
            "invalid": invalid,
            "imu_bad": imu_bad,
            "last_jpeg": now,
            "max_gap": 0.0,
            "texts": [],
        }
        motor_status.set(f"V {motor['sent']} sent. Waiting for the board.")
        print(f"sent V number {motor['sent']} at {now - started:.2f} s", flush=True)

    def finish_window() -> None:
        w = motor["window"]
        motor["window"] = None
        frames = shown - w["shown"]
        samples = imu_count - w["imu"]
        ended = [text for text in w["texts"] if " end " in text]
        print(
            f"after V {motor['sent']}, in {WINDOW_SECONDS} s: frames {frames} ({frames / WINDOW_SECONDS:.1f} fps), "
            f"IMU samples {samples} ({samples / WINDOW_SECONDS:.1f}/s), "
            f"invalid frames {invalid - w['invalid']}, bad IMU frames {imu_bad - w['imu_bad']}, "
            f"longest gap between frames {w['max_gap'] * 1000:.0f} ms",
            flush=True,
        )
        if not w["texts"]:
            print("  the board sent NO motor message for this V", flush=True)
        for text in w["texts"]:
            print(f"  board: {text}", flush=True)
        motor_status.set(
            (ended[-1] if ended else "No 'pulse end' message arrived")
            + f"\nframes {frames}, IMU {samples}, invalid {invalid - w['invalid']} in {WINDOW_SECONDS} s"
        )

    tk.Button(root, text="Vibrate once, 200 ms (V)", command=vibrate).pack(anchor="w")
    root.protocol("WM_DELETE_WINDOW", close)
    root.bind("<KeyPress-q>", close)
    root.bind("<KeyPress-Q>", close)
    root.bind("<KeyPress-v>", vibrate)
    root.bind("<KeyPress-V>", vibrate)
    try:
        while not closed["value"]:
            try:
                frame = reader.next_frame()
            except serial.SerialException as error:
                lost = True
                print(
                    f"*** USB serial connection lost after {time.monotonic() - started:.1f} s. "
                    f"The board reset or was unplugged. {error}",
                    flush=True,
                )
                break
            root.update_idletasks()
            root.update()
            if motor["window"] is not None and time.monotonic() - motor["window"]["t0"] >= WINDOW_SECONDS:
                finish_window()
            if frame is None:
                continue
            kind, payload = frame
            if kind == FRAME_TEXT:
                text_count += 1
                text = payload.decode("utf-8", errors="replace")
                print(text, flush=True)
                if text.startswith("motor"):
                    if motor["window"] is not None:
                        motor["window"]["texts"].append(text)
                    else:
                        motor_status.set(text)
                state = parse_status(text)
                if state is not None and state.get("up_ms", "").isdigit():
                    up_ms = int(state["up_ms"])
                    boot = state.get("boot")
                    reason = state.get("reset", "?")
                    if board["up_ms"] is None:
                        print(f"board: last reset {reason}, boot {boot} since power-on, up {up_ms} ms", flush=True)
                        if reason == "BROWNOUT":
                            print("*** The board's last reset was a BROWNOUT.", flush=True)
                    elif up_ms < board["up_ms"] or boot != board["boot"]:
                        board["restarts"] += 1
                        print(
                            f"*** BOARD RESTARTED while streaming. Reset reason {reason}, boot {boot}, up {up_ms} ms.",
                            flush=True,
                        )
                    board["up_ms"] = up_ms
                    board["boot"] = boot
                    board_status.set(
                        f"reset {reason}  boot {boot}  up {up_ms / 1000:.0f} s  "
                        f"D3 {state.get('d3', '?')}  pulses {state.get('pulses', '?')}  "
                        f"restarts seen {board['restarts']}"
                    )
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
            if motor["window"] is not None:
                w = motor["window"]
                w["max_gap"] = max(w["max_gap"], now - w["last_jpeg"])
                w["last_jpeg"] = now
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
        try:
            port.close()
        except serial.SerialException:
            pass
        root.destroy()
    seconds = time.monotonic() - started
    print(
        f"Stopped after {seconds:.1f} s. Real frames shown: {shown}. Invalid frames: {invalid}. "
        f"Text messages: {text_count}. IMU samples: {imu_count}. Bad IMU frames: {imu_bad}. "
        f"V commands sent: {motor['sent']}. Board restarts seen: {board['restarts']}. "
        f"USB connection lost: {'yes' if lost else 'no'}.",
        flush=True,
    )
    if lost:
        return 3
    return 0 if shown else 2


if __name__ == "__main__":
    raise SystemExit(main())
