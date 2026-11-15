"""IMU preflight and the files a finished PDR test writes.

Calibration and loop tests run inside the one ReTrace service. This module
decides whether the ring is actually streaming, and writes step length or a
loop report when a test ends. It does not start a server.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

PREFLIGHT_MIN_HZ = 80.0

ERR_HOTSPOT = "The ring is not connected (hotspot off)."
ERR_SERIAL = "The ring is not connected (ring on serial)."
ERR_SOCKET = "The ring is not connected (no socket)."
ERR_PICTURES = "The ring is not connected (pictures blocking IMU)."
ERR_RATE = "The ring is not connected (rate too low)."


def measure_imu_hz(link, seconds: float = 1.0) -> tuple[float, int]:
    """Samples per second over one second, and how many IMU samples were dropped in that window.

    The caller has already asked the ring to pause pictures. A short settle
    lets that command land before the window starts.
    """
    time.sleep(0.25)
    status = link.status()
    imu = status.get("imu") or {}
    before = int(imu.get("samples") or 0)
    dropped_before = int(imu.get("dropped") or 0)
    time.sleep(seconds)
    status = link.status()
    imu = status.get("imu") or {}
    after = int(imu.get("samples") or 0)
    dropped_after = int(imu.get("dropped") or 0)
    elapsed = seconds if seconds > 0 else 1.0
    return max(0.0, (after - before) / elapsed), max(0, dropped_after - dropped_before)


def diagnose(
    *,
    imu_hz: float,
    hotspot: bool,
    transport: str,
    connected: bool,
    camera_fps: float,
    dropped_delta: int,
) -> str | None:
    """None when the IMU is at least 80 Hz. Otherwise one plain sentence."""
    if imu_hz >= PREFLIGHT_MIN_HZ:
        return None
    if transport != "wifi":
        return ERR_SERIAL
    if not hotspot:
        return ERR_HOTSPOT
    if not connected:
        return ERR_SOCKET
    if camera_fps > 0 or dropped_delta > 0:
        return ERR_PICTURES
    return ERR_RATE


def preflight_error(link, *, hotspot: bool, seconds: float = 1.0) -> str | None:
    hz, dropped_delta = measure_imu_hz(link, seconds)
    status = link.status()
    return diagnose(
        imu_hz=hz,
        hotspot=hotspot,
        transport=str(status.get("transport") or "serial"),
        connected=bool(status.get("connected")),
        camera_fps=float(status.get("camera_fps") or 0.0),
        dropped_delta=dropped_delta,
    )


def imu_rate_hz(link) -> float:
    """The link's current IMU rate. Used to see whether a pause has settled."""
    status = link.status()
    imu = status.get("imu") or {}
    try:
        return float(imu.get("rate_hz") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def start_block(link, *, hotspot: bool) -> str | None:
    """Refuse Start when the ring is not connected. A low rate waits instead of failing."""
    status = link.status()
    if bool(status.get("connected")):
        return None
    transport = str(status.get("transport") or "serial")
    if transport != "wifi":
        return ERR_SERIAL
    if not hotspot:
        return ERR_HOTSPOT
    return ERR_SOCKET


def describe_drops(events: list[dict], taps: list[dict]) -> str:
    """When samples were lost, measured from the Start and End taps.

    A drop while the walking gate was open is named on its own, not folded into the total.
    """
    rows = [event for event in events if int(event.get("count") or 0) > 0]
    total = sum(int(event["count"]) for event in rows)
    if total <= 0:
        return "No IMU samples were dropped."
    ordered = sorted(taps, key=lambda tap: float(tap.get("unix") or 0.0))
    groups: list[dict] = []
    for event in rows:
        unix = float(event.get("unix") or 0.0)
        anchor = ordered[0] if ordered else {"name": "start", "unix": unix}
        for tap in ordered:
            if float(tap.get("unix") or 0.0) <= unix + 1e-9:
                anchor = tap
            else:
                break
        gate = bool(event.get("gate_open"))
        name = "End" if anchor.get("name") == "end" else "Start"
        delta = max(0.0, unix - float(anchor.get("unix") or 0.0))
        key = (name, gate)
        if groups and groups[-1]["key"] == key:
            groups[-1]["count"] += int(event["count"])
            groups[-1]["lo"] = min(groups[-1]["lo"], delta)
            groups[-1]["hi"] = max(groups[-1]["hi"], delta)
        else:
            groups.append({"key": key, "count": int(event["count"]), "lo": delta, "hi": delta})
    pieces = []
    for group in groups:
        name, gate = group["key"]
        if group["hi"] - group["lo"] < 0.05:
            when = f"at {group['lo']:.1f} s after {name}"
        else:
            when = f"from {group['lo']:.1f} s to {group['hi']:.1f} s after {name}"
        clause = f"{group['count']} {when}"
        if gate:
            clause += ", while the walking gate was open"
        pieces.append(clause)
    text = f"Dropped {total} IMU samples: " + "; ".join(pieces) + "."
    gated = sum(int(event["count"]) for event in rows if event.get("gate_open"))
    if gated:
        text += f" Dropped samples while the walking gate was open: {gated}."
    return text


def write_step_length(path: Path, length: float) -> float:
    """Write only pdr_step_length. Every other key in the file stays as it is."""
    rounded = round(float(length), 3)
    values: dict = {}
    if path.is_file():
        values = json.loads(path.read_text(encoding="utf-8"))
    values["pdr_step_length"] = rounded
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
    return rounded


def write_loop_report(root: Path, run: dict) -> Path:
    """Summary of one finished swing or carry loop. Replay files need real IMU samples."""
    tracker = run["tracker"]
    label = str(run.get("kind") or "swing")
    folder = Path(root) / f"pdr_loop_{label}_{time.strftime('%Y%m%d_%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=True)
    samples = list(run.get("samples") or [])
    p_unix = list(run.get("p_unix") or [])
    summary = {
        "label": label,
        "steps": round(float(tracker.steps), 2),
        "path_m": round(float(tracker.path_m), 2),
        "closure_error_m": run.get("closure_error_m"),
        "p_unix": p_unix if len(p_unix) >= 2 else None,
        "step_count": None,
        "truth_closure_m": 0,
        "imu_dropped": run.get("imu_dropped"),
        "drop_report": run.get("drop_report") or "",
        "marks": len(tracker.marks),
    }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    if len(p_unix) >= 2 and len(samples) >= 20:
        with (folder / "imu.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["host_unix", "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"],
            )
            writer.writeheader()
            for sample in samples:
                writer.writerow(sample)
        meta = {
            "label": label,
            "p_unix": [round(p_unix[0], 4), round(p_unix[1], 4)],
            "step_count": None,
            "truth_closure_m": 0,
        }
        (folder / "loop.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return folder
