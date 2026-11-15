"""Record 30 seconds of walking for the PDR positive case.

Beeps once to say get ready, again when the 30 seconds start, and again when they end.
Count your steps between the start beep and the stop beep.
"""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ring.link import RingLink
from ring.store import IMU_FIELDS

READY_S = 8.0
WALK_S = 30.0


def _beep(freq: int, ms: int) -> None:
    try:
        import winsound

        winsound.Beep(freq, ms)
    except Exception:
        pass


def _write(path: Path, rows: list[dict]) -> None:
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
    deadline = time.time() + 20
    while time.time() < deadline and not link.status()["connected"]:
        time.sleep(0.2)
    if not link.status()["connected"]:
        print(link.status()["error"] or "ring did not connect", flush=True)
        link.stop()
        return 2
    print("GET READY. In a few seconds, walk and count every step you take.", flush=True)
    _beep(660, 150)
    time.sleep(READY_S)
    print("WALK NOW. Count your steps. 30 seconds.", flush=True)
    _beep(990, 400)
    started = time.time()
    seq = 0
    rows: list[dict] = []
    while time.time() - started < WALK_S:
        frame = link.take(seq, timeout=1.0)
        if frame is None:
            continue
        seq = frame["seq"]
        for sample in frame.get("imu_samples") or []:
            if "ax_g" in sample:
                rows.append(dict(sample))
    _beep(520, 250)
    time.sleep(0.1)
    _beep(520, 250)
    print("STOP.", flush=True)
    status = link.status()
    link.stop()
    link.join(timeout=3)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = ROOT / "recordings" / "ring_memory" / "imu" / f"imu-walk30-{stamp}.csv"
    _write(path, rows)
    span = (rows[-1]["host_unix"] - rows[0]["host_unix"]) if len(rows) > 1 else 0.0
    print(f"samples={len(rows)} span_s={span:.1f} camera_fps={status.get('camera_fps')} file={path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
