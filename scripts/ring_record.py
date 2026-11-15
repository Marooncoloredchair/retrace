"""Record ring camera footage and IMU samples for offline mapping tests.

    .\\.venv\\Scripts\\python.exe scripts\\ring_record.py --seconds 60 --label desk-loop

READ-ONLY. It never writes to the serial port, so it cannot flash the board or
run the motor. The port is opened with DTR and RTS off.

Every valid JPEG the board sends is saved unchanged, with every IMU sample:

  recordings/ring_footage/<date-time>[-label]/
    frames/000001.jpg ...   the camera's own JPEG bytes
    frames.csv              one row per frame, with its timestamps
    imu.csv                 one row per IMU sample, raw and scaled
    board_text.log          the board's text lines
    meta.json               counts, sizes, and what the timestamps mean

About the timestamps, because mapping software depends on them:
  host_unix_ns, host_mono_ns   when this laptop finished reading the frame from
                               USB. Not the moment the picture was taken.
  board_ms (imu.csv)           the board's millisecond clock, read right after
                               the IMU registers were read.
  board_ms_before_capture      (frames.csv) the board_ms of the IMU sample the
                               board sent just before it fetched this frame.
                               The firmware does not send a capture time for the
                               picture itself, so this is the closest board-clock
                               value there is, good to about one frame period.

Only one program can hold the XIAO serial port. Close the viewer, the ring
return test, and switch the desktop app back to the iPhone camera first.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import serial  # noqa: E402

import ring_return_test as rrt  # noqa: E402

framing = rrt.framing
ROOT = rrt.ROOT
FOOTAGE_DIR = ROOT / "recordings" / "ring_footage"

FRAME_FIELDS = ("frame", "file", "host_unix_ns", "host_mono_ns", "jpeg_bytes", "board_ms_before_capture", "imu_sample_before")
IMU_FIELDS = (
    "sample", "host_unix_ns", "host_mono_ns", "board_ms", "accel_fs", "gyro_fs",
    "ax_raw", "ay_raw", "az_raw", "temp_raw", "gx_raw", "gy_raw", "gz_raw",
    "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps",
)
TIMESTAMP_NOTES = {
    "host_unix_ns": "laptop wall clock when the frame or sample was read from USB; not the capture time",
    "host_mono_ns": "laptop monotonic clock at the same moment; use this for intervals",
    "board_ms": "board millis() read right after the IMU registers were read",
    "board_ms_before_capture": "board_ms of the IMU sample sent just before this frame was fetched; the firmware sends no capture time for the picture",
}


class Recorder(threading.Thread):
    """Read every frame from the board and write it to disk as it arrives."""

    def __init__(self, port, out_dir: Path) -> None:
        super().__init__(name="ring-record", daemon=True)
        self.port = rrt.ReadOnlyPort(port)
        self.reader = framing.FrameReader(self.port)
        self.out_dir = out_dir
        self.stop_flag = threading.Event()
        self.lock = threading.Lock()
        self.started_mono = time.monotonic()
        self.frames = 0
        self.invalid = 0
        self.imu = 0
        self.imu_bad = 0
        self.texts = 0
        self.width = 0
        self.height = 0
        self.jpeg_bytes = 0
        self.first_frame_mono: float | None = None
        self.last_frame_mono: float | None = None
        self.latest_jpeg: bytes | None = None
        self.first_status: dict | None = None
        self.last_status: dict | None = None
        self.restarts = 0
        self.sensor_line = ""
        self.imu_line = ""
        self.lost = ""
        self.unframed = 0
        self._framed = 0

    def run(self) -> None:
        frames_dir = self.out_dir / "frames"
        frames_dir.mkdir()
        pending_board_ms = ""
        pending_imu = ""
        try:
            with (self.out_dir / "frames.csv").open("w", encoding="utf-8", newline="") as frames_handle, \
                 (self.out_dir / "imu.csv").open("w", encoding="utf-8", newline="") as imu_handle, \
                 (self.out_dir / "board_text.log").open("w", encoding="utf-8") as text_handle:
                frames_csv = csv.writer(frames_handle, lineterminator="\n")
                imu_csv = csv.writer(imu_handle, lineterminator="\n")
                frames_csv.writerow(FRAME_FIELDS)
                imu_csv.writerow(IMU_FIELDS)
                while not self.stop_flag.is_set():
                    try:
                        frame = self.reader.next_frame()
                    except (serial.SerialException, OSError) as error:
                        self.lost = str(error) or error.__class__.__name__
                        break
                    if frame is None:
                        continue
                    unix_ns = time.time_ns()
                    mono_ns = time.monotonic_ns()
                    kind, payload = frame
                    self._framed += framing.HEADER.size + len(payload) + 4
                    if kind == framing.FRAME_TEXT:
                        text = payload.decode("utf-8", errors="replace")
                        text_handle.write(f"{unix_ns} {text}\n")
                        self._text(text)
                    elif kind == framing.FRAME_IMU:
                        sample = framing.decode_imu(payload)
                        if sample is None or not sample["scaled"]:
                            with self.lock:
                                self.imu_bad += 1
                            continue
                        with self.lock:
                            self.imu += 1
                            number = self.imu
                        pending_board_ms = sample["sample_ms"]
                        pending_imu = number
                        imu_csv.writerow(
                            [
                                number, unix_ns, mono_ns, sample["sample_ms"], sample["accel_fs"], sample["gyro_fs"],
                                sample["ax_raw"], sample["ay_raw"], sample["az_raw"], sample["temp_raw"],
                                sample["gx_raw"], sample["gy_raw"], sample["gz_raw"],
                                f"{sample['ax_g']:.5f}", f"{sample['ay_g']:.5f}", f"{sample['az_g']:.5f}",
                                f"{sample['gx_dps']:.3f}", f"{sample['gy_dps']:.3f}", f"{sample['gz_dps']:.3f}",
                            ]
                        )
                    else:
                        image = None
                        if len(payload) >= 4 and payload[0:2] == b"\xff\xd8":
                            image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                        if image is None:
                            with self.lock:
                                self.invalid += 1
                            continue
                        with self.lock:
                            self.frames += 1
                            number = self.frames
                            self.jpeg_bytes += len(payload)
                            self.height, self.width = image.shape[:2]
                            now = mono_ns / 1e9
                            if self.first_frame_mono is None:
                                self.first_frame_mono = now
                            self.last_frame_mono = now
                            self.latest_jpeg = payload
                        name = f"{number:06d}.jpg"
                        (frames_dir / name).write_bytes(payload)
                        frames_csv.writerow(
                            [number, f"frames/{name}", unix_ns, mono_ns, len(payload), pending_board_ms, pending_imu]
                        )
                        pending_board_ms = ""
                        pending_imu = ""
        finally:
            self.unframed = self.port.bytes_read - self._framed - len(self.reader.buffer)
            try:
                self.port.close()
            except Exception:
                pass

    def _text(self, text: str) -> None:
        state = rrt.parse_status(text)
        with self.lock:
            self.texts += 1
            if state is not None and state.get("up_ms", "").isdigit():
                previous = self.last_status
                if previous is None:
                    self.first_status = state
                elif int(state["up_ms"]) < int(previous["up_ms"]) or state.get("boot") != previous.get("boot"):
                    self.restarts += 1
                self.last_status = state
            elif text.startswith("sensor "):
                self.sensor_line = re.sub(r" sent \d+ psram free \d+$", "", text)
            elif text.startswith("imu MPU"):
                self.imu_line = text

    def progress(self) -> dict:
        with self.lock:
            span = 0.0
            if self.first_frame_mono is not None and self.last_frame_mono is not None:
                span = self.last_frame_mono - self.first_frame_mono
            return {
                "frames": self.frames,
                "invalid": self.invalid,
                "imu": self.imu,
                "imu_bad": self.imu_bad,
                "fps": (self.frames - 1) / span if span > 0 and self.frames > 1 else 0.0,
                "restarts": self.restarts,
                "latest_jpeg": self.latest_jpeg,
            }

    def meta(self, port_name: str, label: str, started_iso: str, stopped_by: str) -> dict:
        progress = self.progress()
        span = 0.0
        if self.first_frame_mono is not None and self.last_frame_mono is not None:
            span = self.last_frame_mono - self.first_frame_mono
        return {
            "tool": "scripts/ring_record.py",
            "started": started_iso,
            "label": label,
            "port": port_name,
            "stopped_by": stopped_by,
            "footage_seconds": round(span, 3),
            "frames": progress["frames"],
            "invalid_frames": progress["invalid"],
            "camera_fps": round(progress["fps"], 2),
            "width": self.width,
            "height": self.height,
            "jpeg_bytes_total": self.jpeg_bytes,
            "imu_samples": progress["imu"],
            "imu_bad": progress["imu_bad"],
            "board_text_lines": self.texts,
            "board_restarts": self.restarts,
            "usb_lost": self.lost,
            "bytes_outside_valid_frames": self.unframed,
            "board_status_first": self.first_status,
            "board_status_last": self.last_status,
            "sensor_line": self.sensor_line,
            "imu_line": self.imu_line,
            "timestamps": TIMESTAMP_NOTES,
            "serial_writes": 0,
        }


def run_console(recorder: Recorder, seconds: float) -> str:
    next_print = 1.0
    try:
        while recorder.is_alive():
            elapsed = time.monotonic() - recorder.started_mono
            if seconds and elapsed >= seconds:
                return "time limit"
            if elapsed >= next_print:
                progress = recorder.progress()
                print(
                    f"  {elapsed:5.0f} s   frames {progress['frames']:6d}   IMU {progress['imu']:6d}   "
                    f"{progress['fps']:.1f} frames/s   invalid {progress['invalid']}",
                    flush=True,
                )
                next_print += 5.0 if elapsed >= 10 else 1.0
            time.sleep(0.05)
    except KeyboardInterrupt:
        return "Ctrl+C"
    return f"serial link stopped: {recorder.lost or 'reader ended'}"


def run_window(recorder: Recorder, seconds: float) -> str:
    import tkinter as tk

    root = tk.Tk()
    root.title("ReTrace ring recorder")
    status = tk.StringVar(value="Waiting for the ring camera")
    tk.Label(root, textvariable=status, font=("Segoe UI", 13)).pack(anchor="w", padx=8, pady=4)
    panel = tk.Label(root)
    panel.pack()
    tk.Label(root, text="Press Q or close this window to stop.", font=("Segoe UI", 10)).pack(anchor="w", padx=8, pady=4)
    result = {"stopped_by": ""}

    def stop(reason: str) -> None:
        if not result["stopped_by"]:
            result["stopped_by"] = reason
            root.quit()

    root.bind("<KeyPress-q>", lambda _event: stop("Q key"))
    root.bind("<KeyPress-Q>", lambda _event: stop("Q key"))
    root.protocol("WM_DELETE_WINDOW", lambda: stop("window closed"))

    def tick() -> None:
        if result["stopped_by"]:
            return
        elapsed = time.monotonic() - recorder.started_mono
        if not recorder.is_alive():
            stop(f"serial link stopped: {recorder.lost or 'reader ended'}")
            return
        if seconds and elapsed >= seconds:
            stop("time limit")
            return
        progress = recorder.progress()
        left = f"   {max(0.0, seconds - elapsed):.0f} s left" if seconds else ""
        status.set(
            f"RECORDING  {elapsed:.0f} s{left}   frames {progress['frames']}   IMU {progress['imu']}   "
            f"{progress['fps']:.1f} frames/s"
        )
        if progress["latest_jpeg"] is not None:
            image = cv2.imdecode(np.frombuffer(progress["latest_jpeg"], dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                image = cv2.resize(image, None, fx=2, fy=2, interpolation=cv2.INTER_LINEAR)
                ok, encoded = cv2.imencode(".png", image)
                if ok:
                    photo = tk.PhotoImage(data=encoded.tobytes())
                    panel.configure(image=photo)
                    panel.image = photo
        root.after(100, tick)

    root.after(100, tick)
    root.mainloop()
    try:
        root.destroy()
    except tk.TclError:
        pass
    return result["stopped_by"] or "window closed"


def main() -> int:
    parser = argparse.ArgumentParser(description="Record ring footage and IMU samples. Read-only on the serial port.")
    parser.add_argument("--port", default="", help="serial port, for example COM5. Default: find the XIAO.")
    parser.add_argument("--seconds", type=float, default=60.0, help="how long to record. 0 records until you stop it.")
    parser.add_argument("--label", default="", help="short name added to the folder, for example desk-loop")
    parser.add_argument("--no-window", action="store_true", help="no preview window; progress is printed instead")
    args = parser.parse_args()
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", args.label).strip("-")

    name = args.port or rrt.find_port()
    started = datetime.now()
    out_dir = FOOTAGE_DIR / (started.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else ""))
    print(f"Opening {name} read-only (DTR off, RTS off). Nothing is written to the board.", flush=True)
    try:
        port = framing.open_port(name)
    except serial.SerialException as error:
        print(
            f"Could not open {name}. Close the viewer and the ring return test, and switch the desktop app "
            f"back to the iPhone camera. {error}",
            file=sys.stderr,
        )
        return 1
    out_dir.mkdir(parents=True, exist_ok=False)
    print(f"Recording to {out_dir}", flush=True)
    recorder = Recorder(port, out_dir)
    recorder.start()
    stopped_by = run_console(recorder, args.seconds) if args.no_window else run_window(recorder, args.seconds)
    recorder.stop_flag.set()
    recorder.join(timeout=3)
    meta = recorder.meta(name, label, started.isoformat(timespec="seconds"), stopped_by)
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    megabytes = meta["jpeg_bytes_total"] / 1e6
    print(
        f"\nStopped by: {stopped_by}.\n"
        f"Frames saved {meta['frames']} ({meta['width']}x{meta['height']}, {megabytes:.1f} MB), invalid {meta['invalid_frames']}, "
        f"{meta['camera_fps']} frames/s over {meta['footage_seconds']} s.\n"
        f"IMU samples {meta['imu_samples']}, bad {meta['imu_bad']}. Board restarts {meta['board_restarts']}. "
        f"USB lost: {meta['usb_lost'] or 'no'}.\n"
        f"Next: .\\.venv\\Scripts\\python.exe scripts\\ring_slam_check.py \"{out_dir}\"",
        flush=True,
    )
    return 3 if recorder.lost else (0 if meta["frames"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
