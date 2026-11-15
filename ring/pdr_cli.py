"""PDR calibration, replay, and loop-closure test.

    .\\retrace pdr-calib
    .\\retrace replay-pdr reports\\weartest_20261114_045012
    .\\retrace pdr-loop-test --label swing
    .\\retrace pdr-loop-test --label carry
    .\\retrace pdr-selftest
    .\\retrace pdr-tune
    .\\retrace pdr-report
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import numpy as np

from ring.config import Config, load_config
from ring.pdr import PdrTracker

ROOT = Path(__file__).resolve().parents[1]


def _load_imu_rows(paths: list[Path]) -> list[dict]:
    rows = []
    for path in paths:
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    rows.append(
                        {
                            "host_unix": float(row["host_unix"]),
                            "ax_g": float(row["ax_g"]),
                            "ay_g": float(row["ay_g"]),
                            "az_g": float(row["az_g"]),
                            "gx_dps": float(row["gx_dps"]),
                            "gy_dps": float(row["gy_dps"]),
                            "gz_dps": float(row["gz_dps"]),
                        }
                    )
                except (KeyError, ValueError):
                    continue
    rows.sort(key=lambda sample: sample["host_unix"])
    return rows


def _plot(path: Path, tracker: PdrTracker, title: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"swing": "#3fb37f", "carry": "#e0b24a", "still": "#88929c"}
    fig, ax = plt.subplots(figsize=(8, 7))
    pts = tracker.trace
    for i in range(1, len(pts)):
        ax.plot(
            [pts[i - 1].x, pts[i].x],
            [pts[i - 1].y, pts[i].y],
            color=colors.get(pts[i].mode, "#888"),
            lw=1.4,
        )
    if pts:
        ax.scatter([pts[-1].x], [pts[-1].y], c="#5aa9f0", s=40, label="end", zorder=3)
    for mark in tracker.marks:
        ax.scatter([mark["x"]], [mark["y"]], c="#e0685e", s=36, zorder=4)
        ax.annotate(mark["label"], (mark["x"], mark["y"]), fontsize=8)
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def _write_csv(path: Path, tracker: PdrTracker) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t_s", "x_m", "y_m", "yaw_rad", "mode", "steps", "confidence", "axis_x", "axis_y"])
        for row in tracker.trace:
            writer.writerow(
                [
                    round(row.t, 3),
                    round(row.x, 4),
                    round(row.y, 4),
                    round(row.yaw, 4),
                    row.mode,
                    round(row.steps, 3),
                    round(row.confidence, 3),
                    round(row.axis_x, 4),
                    round(row.axis_y, 4),
                ]
            )


def load_case_rows(case: dict) -> list[dict]:
    """IMU rows for one selftest case, including an optional tail window."""
    rows = _load_imu_rows([ROOT / case["imu"]])
    tail_s = case.get("tail_s")
    if tail_s and rows:
        end = rows[-1]["host_unix"]
        rows = [row for row in rows if row["host_unix"] >= end - float(tail_s)]
    return rows


def _run_rows(config: Config, rows: list[dict]) -> PdrTracker:
    tracker = PdrTracker(config)
    for sample in rows:
        tracker.push(sample)
    return tracker


def imu_report() -> dict:
    """Host rate / units from the newest stored IMU file."""
    folder = ROOT / "recordings" / "ring_memory" / "imu"
    files = sorted(folder.glob("imu-*.csv"))
    if not files:
        return {"error": "no imu csv yet"}
    rows = _load_imu_rows([files[-1]])
    if len(rows) < 5:
        return {"error": "imu file too short", "file": str(files[-1])}
    times = np.array([row["host_unix"] for row in rows])
    dt = np.diff(times)
    dt = dt[(dt > 0) & (dt < 0.5)]
    hz = float(1.0 / np.median(dt)) if len(dt) else 0.0
    return {
        "file": files[-1].name,
        "samples": len(rows),
        "host_hz": round(hz, 2),
        "units": {"accel": "g", "gyro": "deg/s", "time": "host unix seconds"},
        "accel_g_range_seen": {
            "ax": [round(min(r["ax_g"] for r in rows), 2), round(max(r["ax_g"] for r in rows), 2)],
            "ay": [round(min(r["ay_g"] for r in rows), 2), round(max(r["ay_g"] for r in rows), 2)],
            "az": [round(min(r["az_g"] for r in rows), 2), round(max(r["az_g"] for r in rows), 2)],
        },
        "gyro_dps_range_seen": {
            "gx": [round(min(r["gx_dps"] for r in rows), 1), round(max(r["gx_dps"] for r in rows), 1)],
            "gy": [round(min(r["gy_dps"] for r in rows), 1), round(max(r["gy_dps"] for r in rows), 1)],
            "gz": [round(min(r["gz_dps"] for r in rows), 1), round(max(r["gz_dps"] for r in rows), 1)],
        },
        "chip_full_scale": {"accel": "±8 g", "gyro": "±1000 dps"},
        "firmware_period_ms": 10,
        "limit": (
            "The firmware asks for a sample every 20 ms (50 Hz), but IMU reads happen once per "
            "camera loop, and esp_camera_fb_get plus the JPEG USB send blocks that loop. "
            f"The host therefore sees about {hz:.0f} Hz, one sample per VGA frame."
            if hz < 50
            else "Host rate meets 50 Hz."
        ),
    }


def _port_from(argv_port: int) -> int:
    if argv_port:
        return int(argv_port)
    from ring.service import CONFIG_PATH

    config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    return int(config.port)


def _listening(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        try:
            probe.connect(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _service(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    request.add_header("X-Retrace", "1")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode() or "{}")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            payload = {"detail": raw.decode(errors="replace")}
        return error.code, payload
    except urllib.error.URLError:
        return 0, {}


def _require_service(port: int) -> int | None:
    import sys

    if _listening(port):
        return None
    print(f"Start ReTrace first. Nothing is listening on port {port}.", file=sys.stderr)
    return 2


def _enter_pressed() -> bool:
    try:
        import msvcrt
    except ImportError:
        return False
    pressed = False
    while msvcrt.kbhit():
        if msvcrt.getwch() in ("\r", "\n"):
            pressed = True
    return pressed


def _print_test(test: dict) -> None:
    print(
        "steps={steps}  path_m={path_m}  closure_error_m={closure}  imu_dropped={dropped}  step_length={length}".format(
            steps=test.get("steps"),
            path_m=test.get("path_m"),
            closure=test.get("closure_error_m"),
            dropped=test.get("imu_dropped"),
            length=test.get("step_length"),
        ),
        flush=True,
    )
    if test.get("drop_report"):
        print(test["drop_report"], flush=True)
    if test.get("error"):
        print(test["error"], flush=True)


def _ask_metres() -> float:
    try:
        return float(input("Distance walked in metres: ").strip())
    except (ValueError, EOFError):
        return 0.0


def _run_remote(kind: str, port: int, meters: float) -> int:
    import sys

    missing = _require_service(port)
    if missing is not None:
        return missing
    status, body = _service(port, "POST", "/api/pdr/start", {"kind": kind})
    if status != 200:
        print(body.get("detail") or "The ring is not connected.", file=sys.stderr)
        return 2
    print(
        "Pictures are paused. Waiting until the motion stream stays above 80 Hz for one second.",
        flush=True,
    )
    told_walk = False
    try:
        while True:
            if _enter_pressed():
                payload: dict = {}
                if kind == "calib":
                    if meters <= 0:
                        meters = _ask_metres()
                    payload = {"meters": meters}
                status, ended = _service(port, "POST", "/api/pdr/end", payload)
                if status != 200:
                    print(ended.get("detail") or "End failed.", file=sys.stderr)
                    return 1
                if ended.get("phase") == "done":
                    _print_test(ended)
                    return 0 if not ended.get("error") else 1
            status, state = _service(port, "GET", "/api/state")
            test = state.get("pdr_test") or {}
            if not told_walk and test.get("phase") == "walk":
                told_walk = True
                if kind == "calib":
                    print(
                        "Walk a known distance. Type the metres on the phone and press End, "
                        "or press Enter here when you stop.",
                        flush=True,
                    )
                else:
                    print(
                        f"PDR loop test ({kind}). Press End on the phone at the start, walk out and back "
                        "to the same point, and press End again. Enter here marks the next point.",
                        flush=True,
                    )
            if test.get("phase") == "done":
                _print_test(test)
                return 0 if not test.get("error") else 1
            time.sleep(0.4)
    except KeyboardInterrupt:
        _service(port, "POST", "/api/pdr/cancel", {})
        print("\nCancelled. Step length was not changed.", flush=True)
        return 1


def run_pdr_calib(argv: list[str] | None = None) -> int:
    """Talk to the running ReTrace service. Does not start a second server."""
    import argparse

    parser = argparse.ArgumentParser(description="Walk a known distance and set pdr_step_length.")
    parser.add_argument("--meters", type=float, default=0.0, help="distance you walked, in metres")
    parser.add_argument("--port", type=int, default=0, help="dashboard port of the ReTrace service already running")
    args = parser.parse_args(argv)
    return _run_remote("calib", _port_from(args.port), args.meters)



def run_replay_pdr(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Replay stored IMU into a PDR path plot and CSV.")
    parser.add_argument("run_dir")
    args = parser.parse_args(argv)
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        alt = ROOT / "reports" / args.run_dir
        run_dir = alt if alt.is_dir() else run_dir
    if not run_dir.is_dir():
        print(f"Not found: {args.run_dir}", file=sys.stderr)
        return 2
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    session = summary.get("session") or {}
    started = session.get("started_unix_ms")
    ended = session.get("ended_unix_ms")
    imu_dir = ROOT / "recordings" / "ring_memory" / "imu"
    files = sorted(imu_dir.glob("imu-*.csv"))
    rows = _load_imu_rows(files)
    if started and ended:
        lo, hi = started / 1000.0, ended / 1000.0
        rows = [row for row in rows if lo <= row["host_unix"] <= hi]
    if len(rows) < 20:
        print("Not enough IMU samples overlapping this run.", file=sys.stderr)
        return 1
    config = Config(find_backend="pdr")
    tracker = _run_rows(config, rows)
    events_path = run_dir / "events.json"
    if events_path.is_file():
        events = json.loads(events_path.read_text(encoding="utf-8"))
        t0 = rows[0]["host_unix"]
        for event in events.get("hold_events", []):
            if event.get("source") != "marked":
                continue
            when = event["unix_ms"] / 1000.0 - t0
            nearest = min(tracker.trace, key=lambda row: abs(row.t - when), default=None)
            if nearest:
                tracker.marks.append({"label": f"P{event['id']}", "t": nearest.t, "x": nearest.x, "y": nearest.y})
    plot = run_dir / "pdr_path.png"
    table = run_dir / "pdr_trace.csv"
    _plot(plot, tracker, "PDR replay (green swing, orange carry, grey still)")
    _write_csv(table, tracker)
    modes = {}
    for row in tracker.trace:
        modes[row.mode] = modes.get(row.mode, 0) + 1
    report = {
        "samples": len(rows),
        "steps": round(tracker.steps, 2),
        "path_m": round(tracker.path_m, 2),
        "end_xy": [round(float(tracker.pos[0]), 2), round(float(tracker.pos[1]), 2)],
        "windows": modes,
        "plot": plot.name,
        "csv": table.name,
        "imu": imu_report(),
    }
    (run_dir / "replay_pdr.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0



def run_pdr_loop_test(argv: list[str] | None = None) -> int:
    """Talk to the running ReTrace service. Does not start a second server."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Walk out and back. Press End at the start and again at the same point."
    )
    parser.add_argument("--label", choices=("swing", "carry"), default="swing")
    parser.add_argument("--port", type=int, default=0, help="dashboard port of the ReTrace service already running")
    args = parser.parse_args(argv)
    return _run_remote(args.label, _port_from(args.port), 0.0)


SELFTEST_PATH = ROOT / "reports" / "pdr_selftest" / "cases.json"


def _judge(tracker: PdrTracker, case: dict) -> tuple[bool, str]:
    steps = float(tracker.steps)
    path = float(tracker.path_m)
    if case.get("truth_steps") is not None:
        truth = float(case["truth_steps"])
        if case.get("tolerance_steps") is not None:
            limit = float(case["tolerance_steps"])
        else:
            limit = float(case.get("tolerance", 0.10)) * max(truth, 1.0)
        ok = abs(steps - truth) <= limit
        return ok, f"steps={steps:.2f} counted={truth:.0f} allowed=±{limit:.2f} path={path:.2f} m"
    max_steps = float(case.get("max_steps", 3))
    max_path = float(case.get("max_path_m", 2))
    ok = steps <= max_steps and path < max_path
    return ok, f"steps={steps:.2f} (max {max_steps:g}) path={path:.2f} m (under {max_path:g})"


def _plot_gait(path: Path, tracker: PdrTracker, title: str, config: Config) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = tracker.trace
    if not rows:
        return
    t = [row.t for row in rows]
    fig, axes = plt.subplots(2, 1, figsize=(9, 5), sharex=True)
    axes[0].plot(t, [row.acf_peak for row in rows], color="#3f6fb3")
    axes[0].axhline(float(config.pdr_gait_acf_min), color="#c45c26", ls="--", lw=1)
    axes[0].set_ylabel("autocorrelation peak")
    axes[1].plot(t, [row.band_ratio for row in rows], color="#3f6fb3")
    axes[1].axhline(float(config.pdr_gait_band_min), color="#c45c26", ls="--", lw=1)
    axes[1].set_ylabel("band-power ratio")
    axes[1].set_xlabel("time (s)")
    fig.suptitle(title)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def run_pdr_selftest(argv: list[str] | None = None) -> int:
    """Replay the saved negative and, when present, counted-walk recordings."""
    import sys

    if not SELFTEST_PATH.is_file():
        print(f"Missing {SELFTEST_PATH}", file=sys.stderr)
        return 2
    cases = json.loads(SELFTEST_PATH.read_text(encoding="utf-8"))
    config = Config(find_backend="pdr")
    failed = 0
    for case in cases:
        imu = ROOT / case["imu"]
        if not imu.is_file():
            print(f"FAIL  {case['name']}  missing {imu}")
            failed += 1
            continue
        tracker = _run_rows(config, load_case_rows(case))
        ok, detail = _judge(tracker, case)
        print(f"{'PASS' if ok else 'FAIL'}  {case['name']}  {detail}")
        if case.get("print_gates"):
            if not tracker.gate_events:
                print("      gate never opened")
            for kind, when in tracker.gate_events:
                print(f"      gate {kind:5} {when:7.2f} s")
            if tracker.gate_events and tracker.gate_events[-1][0] == "open":
                print(f"      gate still open at {tracker.t:7.2f} s")
        if not ok:
            failed += 1
            plot = ROOT / "reports" / "pdr_selftest" / f"{case['name']}_gait.png"
            _plot_gait(plot, tracker, case["name"], config)
            print(f"      gait plot {plot}")
    return 1 if failed else 0
