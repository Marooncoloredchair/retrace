"""Record IMU rate and socket drops for 10 minutes with pictures paused.

The laptop must not already be listening on 8011. Prints a one-line summary.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ring.link import RingLink

SECONDS = 600
OUT = ROOT / "reports" / "link_hold_10min.json"


def main() -> int:
    link = RingLink(transport="wifi", wifi_port=8011, stall_seconds=30, allow_writes=True)
    link.start()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not link.status()["connected"]:
        time.sleep(0.2)
    if not link.status()["connected"]:
        print("NO_CONNECT")
        link.stop()
        return 2
    link.send_bytes(b"i")
    time.sleep(1.5)
    start = link.status()
    samples = []
    t0 = time.time()
    end = time.monotonic() + SECONDS
    min_hz = 999.0
    while time.monotonic() < end:
        time.sleep(2.0)
        status = link.status()
        imu = status.get("imu") or {}
        board = status.get("board") or {}
        hz = float(imu.get("rate_hz") or 0.0)
        min_hz = min(min_hz, hz)
        samples.append(
            {
                "t": round(time.time() - t0, 1),
                "connected": status.get("connected"),
                "connects": status.get("connects"),
                "disconnects": status.get("disconnects"),
                "hz": hz,
                "samples": imu.get("samples"),
                "dropped": imu.get("dropped"),
                "udp_packets": status.get("udp_packets"),
                "udp_bad": status.get("udp_bad"),
                "up_ms": board.get("up_ms"),
                "boot": board.get("boot"),
                "reset": board.get("reset"),
                "heap": board.get("heap"),
                "rssi": board.get("rssi"),
                "udp_tx": board.get("udp_tx"),
                "udp_fail": board.get("udp_fail"),
                "restarts": status.get("board_restarts"),
            }
        )
        print(
            f"t={samples[-1]['t']:.0f}s hz={hz:.0f} drop={imu.get('dropped')} "
            f"disc={status.get('disconnects')} up={board.get('up_ms')} rssi={board.get('rssi')} heap={board.get('heap')}",
            flush=True,
        )
    link.send_bytes(b"c")
    time.sleep(0.3)
    finish = link.status()
    link.stop()
    imu0 = start.get("imu") or {}
    imu1 = finish.get("imu") or {}
    span = max(1.0, samples[-1]["t"] if samples else 1.0)
    gained = int(imu1.get("samples") or 0) - int(imu0.get("samples") or 0)
    dropped = int(imu1.get("dropped") or 0) - int(imu0.get("dropped") or 0)
    disconnects = int(finish.get("disconnects") or 0) - int(start.get("disconnects") or 0)
    summary = {
        "seconds": round(span, 1),
        "samples_gained": gained,
        "avg_hz": round(gained / span, 1),
        "min_window_hz": round(min_hz, 1),
        "dropped_delta": dropped,
        "disconnects_delta": disconnects,
        "board_restarts": finish.get("board_restarts"),
        "udp_packets": finish.get("udp_packets"),
        "udp_bad": finish.get("udp_bad"),
        "final_board": finish.get("board"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "samples": samples}, indent=2) + "\n", encoding="utf-8")
    print(
        f"RESULT avg_hz={summary['avg_hz']} min_hz={summary['min_window_hz']} "
        f"dropped={dropped} disconnects={disconnects} restarts={summary['board_restarts']}"
    )
    ok = summary["avg_hz"] >= 95 and min_hz >= 95 and disconnects == 0 and dropped == 0
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
