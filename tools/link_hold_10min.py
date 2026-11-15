"""10 minute IMU-only Wi-Fi measurement. Pictures are paused over USB.

Does not bind TCP 8011. Counts UDP batches on 8012 and reads socket drops
from the service already listening on 8010, if one is up.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ring.link import _datagram_frame, load_framing

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 600
OUT = ROOT / "reports" / "link_hold_10min.json"
framing = load_framing()
lock = threading.Lock()
samples_n = 0
packets = 0
bad = 0
gaps = 0
last_status: dict = {}
stop = False


def listen() -> None:
    global samples_n, packets, bad, gaps, last_status
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 8012))
    sock.settimeout(0.5)
    last_counter = None
    while not stop:
        try:
            data, _addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        parsed = _datagram_frame(framing, data)
        with lock:
            if parsed is None:
                bad += 1
                continue
            kind, payload = parsed
            packets += 1
            if kind == framing.FRAME_TEXT:
                text = payload.decode("utf-8", "replace")
                if text.startswith("status "):
                    last_status = parse_tokens(text)
            elif kind == framing.FRAME_IMU_BATCH and len(payload) >= 12:
                count = payload[5]
                first = int.from_bytes(payload[8:12], "little")
                end = first + count
                if last_counter is None:
                    last_counter = end
                elif first > last_counter:
                    gaps += first - last_counter
                    last_counter = end
                elif end > last_counter:
                    last_counter = end
                samples_n += count
    sock.close()


def parse_tokens(text: str) -> dict:
    tokens = text.split()
    if len(tokens) < 3 or len(tokens) % 2 == 0:
        return {}
    return dict(zip(tokens[1::2], tokens[2::2]))


def api() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8010/api/state", timeout=2) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {}


def pause_pictures() -> None:
    import serial

    port = serial.Serial("COM5", 115200, timeout=0.2, dsrdtr=False, rtscts=False)
    port.dtr = False
    port.rts = False
    # The pause byte is read between pictures. Hold the port open and repeat it
    # so a multi-second capture cannot swallow the only copy.
    deadline = time.time() + 12
    while time.time() < deadline:
        port.write(b"i")
        port.flush()
        time.sleep(0.25)
    port.close()


def main() -> int:
    global stop
    time.sleep(8)
    try:
        pause_pictures()
        print("paused pictures", flush=True)
    except Exception as error:
        print("pause failed", type(error).__name__, error, flush=True)
    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    time.sleep(1)
    # Boot and the first picture are not part of the scored window.
    warmup_deadline = time.time() + 20
    last_n = -1
    while time.time() < warmup_deadline:
        time.sleep(1)
        with lock:
            now_n = samples_n
        if now_n > last_n + 80 and last_n >= 0:
            break
        last_n = now_n
    with lock:
        base_samples = samples_n
        base_packets = packets
        base_bad = bad
        base_gaps = gaps
    t0 = time.time()
    rows = []
    first_disc = None
    end = time.time() + SECONDS
    while time.time() < end:
        time.sleep(5)
        state = api()
        link = state.get("link") or {}
        board = link.get("board") or {}
        with lock:
            snap_samples = samples_n - base_samples
            snap_packets = packets - base_packets
            snap_bad = bad - base_bad
            snap_gaps = gaps - base_gaps
            board_udp = dict(last_status)
        elapsed = time.time() - t0
        board = link.get("board") or {}
        if not board.get("up_ms"):
            board = board_udp or board
        row = {
            "t": round(elapsed, 1),
            "samples": snap_samples,
            "packets": snap_packets,
            "bad": snap_bad,
            "gaps": snap_gaps,
            "hz": round(snap_samples / elapsed, 1) if elapsed else 0,
            "connected": link.get("connected"),
            "disconnects": link.get("disconnects"),
            "up_ms": board.get("up_ms"),
            "boot": board.get("boot"),
            "reset": board.get("reset"),
            "heap": board.get("heap"),
            "rssi": board.get("rssi"),
            "udp_tx": board.get("udp_tx"),
            "udp_fail": board.get("udp_fail"),
            "imu_ok": board.get("imu_ok"),
            "imu_fail": board.get("imu_fail"),
            "imu_n": board.get("imu_n"),
            "imu_drop": board.get("imu_drop"),
            "restarts": link.get("board_restarts"),
        }
        if first_disc is None and row["disconnects"] is not None:
            first_disc = row["disconnects"]
        rows.append(row)
        print(
            f"t={row['t']:.0f}s hz={row['hz']} packets={snap_packets} "
            f"disc={row['disconnects']} up={row['up_ms']} rssi={row['rssi']} "
            f"udp_tx={row['udp_tx']} fail={row['udp_fail']} gaps={snap_gaps} "
            f"imu_ok={row['imu_ok']} imu_fail={row['imu_fail']} imu_n={row['imu_n']} "
            f"imu_drop={row['imu_drop']}",
            flush=True,
        )
    stop = True
    thread.join(timeout=2)
    elapsed = max(1.0, rows[-1]["t"] if rows else 1.0)
    # Window rate from the last 10 minutes of sample deltas, and the lowest 10 s slice.
    min_slice = 999.0
    for i, row in enumerate(rows):
        prev = rows[i - 2] if i >= 2 else rows[0]
        dt = row["t"] - prev["t"]
        if dt >= 8:
            min_slice = min(min_slice, (row["samples"] - prev["samples"]) / dt)
    last_disc = rows[-1]["disconnects"] if rows else None
    disc_delta = None if first_disc is None or last_disc is None else int(last_disc) - int(first_disc)
    ups = [int(r["up_ms"]) for r in rows if r.get("up_ms")]
    rewound = any(ups[i] + 1500 < ups[i - 1] for i in range(1, len(ups)))
    summary = {
        "seconds": round(elapsed, 1),
        "samples": rows[-1]["samples"] if rows else 0,
        "avg_hz": round((rows[-1]["samples"] if rows else 0) / elapsed, 1),
        "min_10s_hz": round(min_slice, 1) if min_slice < 900 else None,
        "udp_packets": rows[-1]["packets"] if rows else 0,
        "udp_bad": rows[-1]["bad"] if rows else 0,
        "sample_gaps": rows[-1]["gaps"] if rows else 0,
        "disconnects_delta": disc_delta,
        "uptime_rewound": rewound,
        "boot": rows[-1].get("boot") if rows else None,
        "reset": rows[-1].get("reset") if rows else None,
        "heap": rows[-1].get("heap") if rows else None,
        "rssi": rows[-1].get("rssi") if rows else None,
    }
    OUT.write_text(json.dumps({"summary": summary, "samples": rows}, indent=2) + "\n", encoding="utf-8")
    print("RESULT", json.dumps(summary), flush=True)
    ok = (
        summary["avg_hz"] >= 95
        and (summary["min_10s_hz"] or 0) >= 95
        and disc_delta == 0
        and (summary["sample_gaps"] or 0) == 0
        and not rewound
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
