"""Import a USB dump from the standalone IMU logger and replay each marked loop.

    .\\retrace pdr-import
    .\\retrace pdr-import --listen-s 30
    .\\retrace pdr-import --file dump.bin
"""

from __future__ import annotations

import argparse
import json
import struct
import time
from dataclasses import replace
from pathlib import Path

from ring.config import load_config
from ring.pdr import HEADING_METHODS
from ring.pdr_cli import ROOT
from ring.pdr_tune import replay_loop
from ring.service import CONFIG_PATH

SAMPLE_STRUCT = struct.Struct("<I6h")
MARK_STRUCT = struct.Struct("<I")
ACCEL_LSB_PER_G = 4096.0
GYRO_LSB_PER_DPS = 32.8


class DumpError(Exception):
    pass


def parse_dump(blob: bytes) -> dict:
    """Parse a RETRACE_LOG v1 dump into samples and mark timestamps."""
    start = blob.find(b"RETRACE_LOG v1\n")
    if start < 0:
        raise DumpError("dump is not RETRACE_LOG v1")
    blob = blob[start:]
    header, _, rest = blob.partition(b"END_HEADER\n")
    if not header.startswith(b"RETRACE_LOG v1\n") or not rest:
        raise DumpError("dump is not RETRACE_LOG v1")
    fields: dict[str, str] = {}
    for line in header.decode("ascii", errors="replace").splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2:
            fields[parts[0]] = parts[1]
    try:
        sample_count = int(fields["samples"])
        mark_count = int(fields["marks"])
        period_us = int(fields.get("period_us", "10000"))
        t0_us = int(fields.get("t0_us", "0"))
        accel_lsb = float(fields.get("accel_lsb_per_g", ACCEL_LSB_PER_G))
        gyro_lsb = float(fields.get("gyro_lsb_per_dps", GYRO_LSB_PER_DPS))
    except (KeyError, ValueError) as error:
        raise DumpError(f"dump header is incomplete: {error}") from error
    sample_bytes = sample_count * SAMPLE_STRUCT.size
    mark_bytes = mark_count * MARK_STRUCT.size
    need = sample_bytes + mark_bytes
    if len(rest) < need + len(b"END\n"):
        raise DumpError(
            f"dump body is {len(rest)} bytes, need {need + 4} for {sample_count} samples and {mark_count} marks"
        )
    raw_samples = rest[:sample_bytes]
    raw_marks = rest[sample_bytes:need]
    if rest[need : need + 4] != b"END\n":
        raise DumpError("dump is missing END")
    tail = rest[need + 4 :]
    samples = []
    for index in range(sample_count):
        t_us, ax, ay, az, gx, gy, gz = SAMPLE_STRUCT.unpack_from(raw_samples, index * SAMPLE_STRUCT.size)
        samples.append(
            {
                "t_us": int(t_us),
                "ax": int(ax),
                "ay": int(ay),
                "az": int(az),
                "gx": int(gx),
                "gy": int(gy),
                "gz": int(gz),
            }
        )
    marks = [MARK_STRUCT.unpack_from(raw_marks, index * MARK_STRUCT.size)[0] for index in range(mark_count)]
    return {
        "samples": samples,
        "marks_us": [int(item) for item in marks],
        "period_us": period_us,
        "t0_us": t0_us,
        "accel_lsb_per_g": accel_lsb,
        "gyro_lsb_per_dps": gyro_lsb,
        "full": fields.get("full", "0") == "1",
        "dropped": int(fields.get("dropped", "0") or 0),
        "imu_fail": int(fields.get("imu_fail", "0") or 0),
        "tail": tail,
    }


def sample_rate_hz(samples: list[dict]) -> float:
    if len(samples) < 2:
        return 0.0
    span_us = int(samples[-1]["t_us"]) - int(samples[0]["t_us"])
    if span_us <= 0:
        return 0.0
    return (len(samples) - 1) * 1_000_000.0 / span_us


def to_rows(parsed: dict, wall0: float | None = None) -> list[dict]:
    """Existing loop-recording rows. host_unix is seconds on the same clock as the marks."""
    samples = parsed["samples"]
    if wall0 is None:
        if samples:
            wall0 = time.time() - (int(samples[-1]["t_us"]) / 1_000_000.0)
        else:
            wall0 = time.time()
    accel = float(parsed["accel_lsb_per_g"]) or ACCEL_LSB_PER_G
    gyro = float(parsed["gyro_lsb_per_dps"]) or GYRO_LSB_PER_DPS
    rows = []
    for sample in samples:
        rows.append(
            {
                "host_unix": wall0 + int(sample["t_us"]) / 1_000_000.0,
                "t_us": int(sample["t_us"]),
                "ax_g": sample["ax"] / accel,
                "ay_g": sample["ay"] / accel,
                "az_g": sample["az"] / accel,
                "gx_dps": sample["gx"] / gyro,
                "gy_dps": sample["gy"] / gyro,
                "gz_dps": sample["gz"] / gyro,
            }
        )
    return rows


def loop_pairs(marks_us: list[int]) -> list[tuple[int, int]]:
    """One loop for each successive pair of marks: (0, 1), (1, 2), ..."""
    return [(int(marks_us[index]), int(marks_us[index + 1])) for index in range(len(marks_us) - 1)]


def slice_loop(rows: list[dict], start_us: int, end_us: int) -> list[dict]:
    return [row for row in rows if start_us <= int(row["t_us"]) <= end_us]


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["host_unix", "ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row[key] for key in writer.fieldnames})


def save_recording(parsed: dict, out_dir: Path) -> dict:
    """Write the whole log and one existing-format folder per mark pair."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = to_rows(parsed)
    _write_csv(out_dir / "imu.csv", rows)
    mark_rows = []
    wall0 = rows[0]["host_unix"] - rows[0]["t_us"] / 1_000_000.0 if rows else time.time()
    for index, t_us in enumerate(parsed["marks_us"], start=1):
        mark_rows.append({"index": index, "t_us": int(t_us), "host_unix": wall0 + int(t_us) / 1_000_000.0})
    (out_dir / "marks.json").write_text(json.dumps(mark_rows, indent=2) + "\n", encoding="utf-8")
    loops = []
    for index, (start_us, end_us) in enumerate(loop_pairs(parsed["marks_us"]), start=1):
        folder = out_dir / f"loop_{index:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        chosen = slice_loop(rows, start_us, end_us)
        _write_csv(folder / "imu.csv", chosen)
        p_unix = [wall0 + start_us / 1_000_000.0, wall0 + end_us / 1_000_000.0]
        meta = {
            "label": "import",
            "p_unix": [round(p_unix[0], 4), round(p_unix[1], 4)],
            "step_count": None,
            "truth_closure_m": 0,
        }
        (folder / "loop.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        loops.append(
            {
                "name": folder.name,
                "label": "import",
                "folder": folder,
                "rows": chosen,
                "p_unix": (p_unix[0], p_unix[1]),
                "t0": chosen[0]["host_unix"] if chosen else p_unix[0],
                "start_us": start_us,
                "end_us": end_us,
            }
        )
    return {"dir": out_dir, "rows": rows, "loops": loops, "rate_hz": sample_rate_hz(parsed["samples"])}


def heading_closures(config, loop, known: float | None = None) -> dict[str, float]:
    """Closure of one loop under swing, gyro, and hybrid heading."""
    current = getattr(config, "pdr_heading_method", "swing")
    closures = {}
    for method in HEADING_METHODS:
        if known is not None and method == current:
            closures[method] = round(float(known), 3)
            continue
        _tracker, closure = replay_loop(replace(config, pdr_heading_method=method), loop)
        closures[method] = round(float(closure), 3)
    return closures


def replay_recording(saved: dict, config) -> list[dict]:
    results = []
    for loop in saved["loops"]:
        if len(loop["rows"]) < 2:
            results.append(
                {
                    "name": loop["name"],
                    "steps": 0.0,
                    "path_m": 0.0,
                    "closure_error_m": None,
                    "closures_m": {method: None for method in HEADING_METHODS},
                    "tracker": None,
                    "samples": len(loop["rows"]),
                }
            )
            continue
        tracker, closure = replay_loop(config, loop)
        closures = heading_closures(config, loop, known=closure)
        results.append(
            {
                "name": loop["name"],
                "steps": round(float(tracker.steps), 2),
                "path_m": round(float(tracker.path_m), 2),
                "closure_error_m": round(float(closure), 3),
                "closures_m": closures,
                "tracker": tracker,
                "samples": len(loop["rows"]),
            }
        )
    return results


def draw_paths(results: list[dict], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8, 7))
    colors = ["#3fb37f", "#e0b24a", "#5aa9f0", "#e0685e", "#9b7bdb"]
    drew = False
    for index, result in enumerate(results):
        tracker = result.get("tracker")
        if tracker is None or not tracker.trace:
            continue
        pts = tracker.trace
        xs = [point.x for point in pts]
        ys = [point.y for point in pts]
        closure = result["closure_error_m"]
        label = f"{result['name']}  {closure:.2f} m" if closure is not None else result["name"]
        ax.plot(xs, ys, color=colors[index % len(colors)], lw=1.4, label=label)
        ax.scatter([xs[0]], [ys[0]], c=colors[index % len(colors)], s=28, zorder=3)
        drew = True
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title("Imported loops")
    if drew:
        ax.legend()
    else:
        ax.text(0.5, 0.5, "no path", ha="center", va="center", transform=ax.transAxes)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def read_dump_from_port(port: str, listen_s: float) -> bytes:
    import serial

    link = serial.Serial()
    link.dtr = False
    link.rts = False
    link.port = port
    link.baudrate = 115200
    link.timeout = 0.2
    link.open()
    try:
        deadline = time.time() + max(0.0, listen_s)
        while time.time() < deadline:
            chunk = link.read(4096)
            if chunk:
                text = chunk.decode("ascii", errors="replace")
                for line in text.splitlines():
                    if line.startswith("logger ") or line.startswith("imu ") or line.startswith("mark "):
                        print(line)
        link.reset_input_buffer()
        link.write(b"D")
        link.flush()
        blob = bytearray()
        quiet = time.time()
        while b"END_HEADER\n" not in blob and time.time() - quiet < 3.0:
            chunk = link.read(4096)
            if chunk:
                blob.extend(chunk)
                quiet = time.time()
        marker = blob.find(b"END_HEADER\n")
        if marker < 0:
            return bytes(blob)
        header = bytes(blob[: marker + len(b"END_HEADER\n")])
        fields = {}
        for line in header.decode("ascii", errors="replace").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2:
                fields[parts[0]] = parts[1]
        try:
            need = int(fields.get("samples", "0")) * SAMPLE_STRUCT.size
            need += int(fields.get("marks", "0")) * MARK_STRUCT.size
        except ValueError as error:
            raise DumpError(f"dump header is incomplete: {error}") from error
        body = bytearray(blob[marker + len(b"END_HEADER\n") :])
        quiet = time.time()
        while len(body) < need + len(b"END\n") and time.time() - quiet < 5.0:
            chunk = link.read(8192)
            if chunk:
                body.extend(chunk)
                quiet = time.time()
        return header + bytes(body)
    finally:
        link.close()


def run_pdr_import(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump the IMU logger and replay each pair of marks.")
    parser.add_argument("--port", default="COM5", help="USB serial port (default COM5)")
    parser.add_argument("--file", default="", help="parse a dump file instead of reading the ring")
    parser.add_argument("--listen-s", type=float, default=0.0, help="read status for this many seconds before sending D")
    parser.add_argument("--out", default="", help="output folder (default reports/pdr_import_<time>)")
    args = parser.parse_args(argv)
    if args.file:
        blob = Path(args.file).read_bytes()
    else:
        blob = read_dump_from_port(args.port, args.listen_s)
    try:
        parsed = parse_dump(blob)
    except DumpError as error:
        print(error, flush=True)
        return 1
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else ROOT / "reports" / f"pdr_import_{stamp}"
    (out_dir if args.out else out_dir).mkdir(parents=True, exist_ok=True)
    (out_dir / "logger_dump.bin").write_bytes(blob)
    saved = save_recording(parsed, out_dir)
    config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    results = replay_recording(saved, config)
    image = out_dir / "paths.png"
    draw_paths(results, image)
    print(
        f"samples={len(parsed['samples'])} rate_hz={saved['rate_hz']:.2f} "
        f"marks={len(parsed['marks_us'])} dropped={parsed['dropped']} imu_fail={parsed['imu_fail']}"
    )
    if not results:
        print("no loop: a loop is the span between two consecutive marks")
    for result in results:
        closures = result.get("closures_m") or {}
        parts = []
        for method in HEADING_METHODS:
            value = closures.get(method)
            parts.append(f"closure_{method}_m={'none' if value is None else f'{value:.3f}'}")
        print(
            f"{result['name']} samples={result['samples']} steps={result['steps']:.2f} "
            f"path_m={result['path_m']:.2f} " + " ".join(parts)
        )
    print(f"wrote {out_dir}")
    print(f"wrote {image}")
    return 0
