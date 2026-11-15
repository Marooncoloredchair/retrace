"""Record the ring after the FIFO flash and print the IMU checks.

Phases, announced with a beep:
  12 s  hold the ring still
  12 s  turn it slowly through 90 degrees and hold
  120 s walk with the arm swinging, and count your steps

Prints rate, drops, camera fps, still gravity, turn yaw, and walking axis ranges.
Writes the walk to the IMU log and a report folder for replay-pdr.
"""

from __future__ import annotations

import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring.config import Config
from ring.link import RingLink
from ring.pdr import PdrTracker
from ring.store import IMU_FIELDS

STILL_S = 12.0
TURN_S = 12.0
WALK_S = 120.0


def _beep() -> None:
    try:
        import winsound

        winsound.Beep(880, 180)
    except Exception:
        pass


def _span(rows: list[dict], key: str) -> list[float]:
    vals = [float(row[key]) for row in rows if row.get(key) is not None]
    if not vals:
        return []
    return [round(min(vals), 3), round(max(vals), 3)]


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(IMU_FIELDS)
        for sample in rows:
            writer.writerow(
                [
                    f"{sample.get('host_unix', 0):.4f}",
                    sample.get("sample_ms", ""),
                    *(f"{sample[key]:.5f}" if isinstance(sample.get(key), float) else "" for key in IMU_FIELDS[2:9]),
                    *(sample.get(key, "") for key in IMU_FIELDS[9:]),
                ]
            )


def main() -> int:
    link = RingLink(stall_seconds=8.0)
    link.start()
    print("Waiting for the ring...", flush=True)
    deadline = time.time() + 25
    while time.time() < deadline and not link.status()["connected"]:
        time.sleep(0.2)
    if not link.status()["connected"]:
        print(link.status()["error"] or "ring did not connect")
        link.stop()
        return 2
    print(link.status()["imu"].get("line") or "", flush=True)
    print("\nHOLD THE RING STILL.", flush=True)
    _beep()
    started = time.time()
    seq = 0
    frames = 0
    rows: list[dict] = []
    phase = "still"
    announced = {"turn": False, "walk": False}
    while True:
        elapsed = time.time() - started
        if elapsed >= STILL_S + TURN_S + WALK_S:
            break
        if elapsed >= STILL_S + TURN_S and not announced["walk"]:
            announced["walk"] = True
            phase = "walk"
            print("\nWALK. Swing the arm the way you do when carrying the ring. Count your steps.", flush=True)
            _beep()
        elif elapsed >= STILL_S and not announced["turn"]:
            announced["turn"] = True
            phase = "turn"
            print("\nTURN THE RING SLOWLY THROUGH 90 DEGREES, THEN HOLD.", flush=True)
            _beep()
        frame = link.take(seq, timeout=1.0)
        if frame is None:
            continue
        seq = frame["seq"]
        frames += 1
        for sample in frame.get("imu_samples") or []:
            if "ax_g" not in sample:
                continue
            sample = dict(sample)
            sample["phase"] = phase
            rows.append(sample)
    _beep()
    status = link.status()
    link.stop()
    link.join(timeout=3)

    still = [row for row in rows if row["phase"] == "still" and row["host_unix"] >= rows[0]["host_unix"] + 2.0]
    turn = [row for row in rows if row["phase"] == "turn"]
    walk = [row for row in rows if row["phase"] == "walk"]
    walk_30 = []
    if walk:
        t0 = walk[0]["host_unix"]
        walk_30 = [row for row in walk if row["host_unix"] <= t0 + 30.0]

    def rate(group: list[dict]) -> float:
        if len(group) < 2:
            return 0.0
        counters = [int(row["sample_counter"]) for row in group if row.get("sample_counter") is not None]
        span = group[-1]["host_unix"] - group[0]["host_unix"]
        if counters and span > 0:
            return (counters[-1] - counters[0]) / span
        return (len(group) - 1) / span if span > 0 else 0.0

    dropped = 0
    last = None
    for row in rows:
        counter = row.get("sample_counter")
        if counter is None:
            continue
        counter = int(counter)
        if last is not None and counter > last + 1:
            dropped += counter - last - 1
        last = counter

    mag = 0.0
    if still:
        mag = float(np.median([math.sqrt(r["ax_g"] ** 2 + r["ay_g"] ** 2 + r["az_g"] ** 2) for r in still]))

    yaw_deg = None
    if still and turn:
        tracker = PdrTracker(Config(find_backend="pdr"))
        turn_start = turn[0]["host_unix"]
        yaw_at_start = None
        for row in still + turn:
            tracker.push(row)
            if row["host_unix"] >= turn_start and yaw_at_start is None:
                yaw_at_start = tracker.yaw
        if yaw_at_start is not None:
            delta = (tracker.yaw - yaw_at_start + math.pi) % (2 * math.pi) - math.pi
            yaw_deg = round(math.degrees(delta), 1)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    report_dir = ROOT / "reports" / f"imu_check_{stamp}"
    imu_dir = ROOT / "recordings" / "ring_memory" / "imu"
    imu_dir.mkdir(parents=True, exist_ok=True)
    csv_path = imu_dir / f"imu-{stamp.replace('_', '-')}-fifo.csv"
    _write_csv(csv_path, walk)
    if walk:
        summary = {
            "session": {
                "started_unix_ms": int(walk[0]["host_unix"] * 1000),
                "ended_unix_ms": int(walk[-1]["host_unix"] * 1000),
            }
        }
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    limits = {"accel_g": 8, "gyro_dps": 1000}
    near = []
    for row in walk_30:
        for key, limit in (("ax_g", 8), ("ay_g", 8), ("az_g", 8), ("gx_dps", 1000), ("gy_dps", 1000), ("gz_dps", 1000)):
            if abs(float(row[key])) > 0.8 * limit:
                near.append(key)
                break
    report = {
        "imu_line": status["imu"].get("line"),
        "fifo_order": status["imu"].get("fifo_order"),
        "accel_fs": None if not rows else rows[-1].get("accel_fs"),
        "gyro_fs": None if not rows else rows[-1].get("gyro_fs"),
        "samples": len(rows),
        "host_hz_all": round(rate(rows), 2),
        "host_hz_walk": round(rate(walk), 2),
        "dropped_samples": dropped,
        "camera_fps": status.get("camera_fps"),
        "camera_frames": frames,
        "still_samples": len(still),
        "still_accel_magnitude_g": round(mag, 3),
        "turn_yaw_deg": yaw_deg,
        "walk_30s_minmax": {
            "ax_g": _span(walk_30, "ax_g"),
            "ay_g": _span(walk_30, "ay_g"),
            "az_g": _span(walk_30, "az_g"),
            "gx_dps": _span(walk_30, "gx_dps"),
            "gy_dps": _span(walk_30, "gy_dps"),
            "gz_dps": _span(walk_30, "gz_dps"),
        },
        "walk_samples_near_80pct_of_full_scale": len(near),
        "full_scale": limits,
        "walk_csv": str(csv_path),
        "report_dir": str(report_dir),
    }
    if report_dir.is_dir():
        (report_dir / "imu_check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
