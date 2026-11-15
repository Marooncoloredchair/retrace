"""Replay saved walk-loops, search PDR settings, and draw them together.

    .\\retrace pdr-tune
    .\\retrace pdr-tune --apply
    .\\retrace pdr-report
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from ring.config import Config
from ring.pdr import PdrTracker, hop_features, smooth_heading, world_samples
from ring.pdr_cli import ROOT, SELFTEST_PATH, _judge, _load_imu_rows, load_case_rows

# Shape of the path (needs a full hop replay) and the two scales applied afterwards.
SHAPE_GRID = {
    "pdr_gait_acf_min": (0.08, 0.12, 0.20),
    "pdr_gait_band_min": (0.22, 0.28, 0.40),
    "pdr_gait_stop_s": (1.5, 2.0, 3.0),
    "pdr_swing_confidence": (1.8, 2.5, 4.0),
    "pdr_window_s": (1.5, 2.0, 3.0),
}
SCALE_GRID = {
    "pdr_step_length": (0.55, 0.70, 0.85),
    "pdr_heading_smooth": (0.0, 0.4, 0.75),
}
TUNED_KEYS = tuple(SHAPE_GRID) + tuple(SCALE_GRID)

LOOP_GLOB = "pdr_loop_*/loop.json"


def load_loops(root: Path | None = None) -> list[dict]:
    """Saved pdr-loop-test recordings that include IMU and two P timestamps."""
    root = root or ROOT
    found = []
    for meta_path in sorted((root / "reports").glob(LOOP_GLOB)):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        imu = meta_path.parent / "imu.csv"
        stamps = meta.get("p_unix") or []
        if meta.get("label") not in ("swing", "carry") or len(stamps) < 2 or not imu.is_file():
            continue
        rows = _load_imu_rows([imu])
        if len(rows) < 20:
            continue
        found.append(
            {
                "name": meta_path.parent.name,
                "label": meta["label"],
                "p_unix": (float(stamps[0]), float(stamps[1])),
                "step_count": meta.get("step_count"),
                "rows": rows,
                "t0": rows[0]["host_unix"],
            }
        )
    return found


def _commits(hops: list[dict], config: Config) -> list[tuple]:
    tracker = PdrTracker(config)
    tracker._commit_log = []
    for feats in hops:
        tracker.apply_hop(feats)
    return list(tracker._commit_log)


def _path_from_commits(commits: list[tuple], step_length: float, smooth: float) -> tuple[float, float, list[tuple]]:
    """Return (steps, path_m, [(t, x, y), ...]) for one step length and heading blend."""
    pos = np.zeros(2)
    previous = None
    points = []
    path = 0.0
    steps = 0.0
    for item in commits:
        t, steps_now, hx, hy, unit = item
        heading = smooth_heading(previous, np.array([hx, hy], dtype=np.float64), smooth)
        previous = heading
        delta = heading * (float(steps_now) * float(unit) * float(step_length))
        pos = pos + delta
        path += float(np.linalg.norm(delta))
        steps += float(steps_now)
        points.append((float(t), float(pos[0]), float(pos[1])))
    return steps, path, points


def _xy_at(points: list[tuple], when: float) -> tuple[float, float]:
    chosen = (0.0, 0.0)
    for t, x, y in points:
        if t <= when + 1e-9:
            chosen = (x, y)
        else:
            break
    return chosen


def _closure(points: list[tuple], t0: float, p_unix: tuple[float, float]) -> float:
    a = _xy_at(points, p_unix[0] - t0)
    b = _xy_at(points, p_unix[1] - t0)
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def _case_ok(steps: float, path: float, case: dict) -> bool:
    ok, _detail = _judge(SimpleNamespace(steps=steps, path_m=path), case)
    return ok


def search(
    loops: list[dict],
    cases: list[dict],
    shape_grid: dict | None = None,
    scale_grid: dict | None = None,
    methods: tuple[str, ...] = ("swing",),
    step_tolerance: float = 3.0,
) -> list[dict]:
    """Rank the grid. Each result includes per-loop closure and whether the selftests passed."""
    shape_grid = shape_grid or SHAPE_GRID
    scale_grid = scale_grid or SCALE_GRID
    tables: dict[str, dict[float, list[dict]]] = {}
    for case in cases:
        rows = load_case_rows(case)
        series = world_samples(rows, Config(find_backend="pdr"))
        tables[case["name"]] = {}
        for window in shape_grid["pdr_window_s"]:
            cfg = Config(find_backend="pdr", pdr_window_s=window)
            tables[case["name"]][window] = hop_features(series, cfg)
    loop_tables = []
    for loop in loops:
        series = world_samples(loop["rows"], Config(find_backend="pdr"))
        by_window = {}
        for window in shape_grid["pdr_window_s"]:
            cfg = Config(find_backend="pdr", pdr_window_s=window)
            by_window[window] = hop_features(series, cfg)
        loop_tables.append(by_window)

    shape_keys = list(shape_grid)
    ranked = []
    for values in product(*(shape_grid[key] for key in shape_keys)):
        shape = dict(zip(shape_keys, values))
        cfg = Config(find_backend="pdr", **shape)
        case_logs = {
            case["name"]: _commits(tables[case["name"]][shape["pdr_window_s"]], cfg) for case in cases
        }
        # Steps do not depend on the heading method. Closure does.
        loop_logs = {
            method: [
                _commits(table[shape["pdr_window_s"]], replace(cfg, pdr_heading_method=method))
                for table in loop_tables
            ]
            for method in methods
        }
        for length, smooth in product(scale_grid["pdr_step_length"], scale_grid["pdr_heading_smooth"]):
            setting = {**shape, "pdr_step_length": length, "pdr_heading_smooth": smooth}
            case_scores = {}
            passed = True
            for case in cases:
                steps, path, _points = _path_from_commits(case_logs[case["name"]], length, smooth)
                case_scores[case["name"]] = {"steps": steps, "path_m": path}
                if not _case_ok(steps, path, case):
                    passed = False
            by_method = {}
            step_misses = 0
            for method in methods:
                per_loop = []
                for loop, commits in zip(loops, loop_logs[method]):
                    loop_steps, _path, points = _path_from_commits(commits, length, smooth)
                    per_loop.append(
                        {
                            "name": loop["name"],
                            "label": loop["label"],
                            "steps": loop_steps,
                            "closure_m": _closure(points, loop["t0"], loop["p_unix"]),
                            "truth_steps": loop.get("truth_steps"),
                        }
                    )
                closures = [row["closure_m"] for row in per_loop]
                by_method[method] = {
                    "loops": per_loop,
                    "median_closure_m": float(np.median(closures)) if closures else float("inf"),
                    "mean_closure_m": float(np.mean(closures)) if closures else float("inf"),
                }
            if by_method:
                first = by_method[methods[0]]["loops"]
                for row in first:
                    truth = row.get("truth_steps")
                    if truth is not None and abs(float(row["steps"]) - float(truth)) > float(step_tolerance):
                        step_misses += 1
            best_method = min(methods, key=lambda name: by_method[name]["median_closure_m"])
            best = by_method[best_method]
            ranked.append(
                {
                    "setting": setting,
                    "mean_closure_m": best["mean_closure_m"],
                    "median_closure_m": best["median_closure_m"],
                    "best_method": best_method,
                    "step_misses": step_misses,
                    "loops": best["loops"],
                    "methods": by_method,
                    "cases": case_scores,
                    "passed": passed,
                }
            )
    ranked.sort(
        key=lambda row: (
            not row["passed"],
            row["step_misses"],
            row["median_closure_m"],
            row["mean_closure_m"],
            tuple(row["setting"][key] for key in TUNED_KEYS),
        )
    )
    return ranked


def format_setting(setting: dict) -> str:
    return (
        f"acf {setting['pdr_gait_acf_min']:.2f}  "
        f"band {setting['pdr_gait_band_min']:.2f}  "
        f"stop {setting['pdr_gait_stop_s']:.1f} s  "
        f"swing confidence {setting['pdr_swing_confidence']:.1f}  "
        f"window {setting['pdr_window_s']:.1f} s  "
        f"step {setting['pdr_step_length']:.2f} m  "
        f"heading smooth {setting['pdr_heading_smooth']:.2f}"
    )


def write_setting(setting: dict, path: Path) -> None:
    """Merge the tuned keys into ring_config.json. Other keys, including find_backend, stay."""
    values = {}
    if path.is_file():
        values = json.loads(path.read_text(encoding="utf-8"))
    for key in TUNED_KEYS:
        value = setting[key]
        values[key] = int(value) if isinstance(value, float) and value.is_integer() else round(float(value), 4)
    path.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")


def run_pdr_tune(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from ring.service import CONFIG_PATH

    parser = argparse.ArgumentParser(
        description="Search PDR settings on saved loops. Writes ring_config.json only with --apply."
    )
    parser.add_argument("--apply", action="store_true", help="write the best passing setting into ring_config.json")
    args = parser.parse_args(argv)
    if not SELFTEST_PATH.is_file():
        print(f"Missing {SELFTEST_PATH}", file=sys.stderr)
        return 2
    cases = json.loads(SELFTEST_PATH.read_text(encoding="utf-8"))
    loops = load_loops(ROOT)
    if not loops:
        print("No saved loop recordings. Run pdr-loop-test, press P at the start and again at the same point.", file=sys.stderr)
        return 1
    n_shape = math.prod(len(values) for values in SHAPE_GRID.values())
    n_scale = math.prod(len(values) for values in SCALE_GRID.values())
    print(
        f"Scoring {n_shape * n_scale} settings on {len(loops)} loop(s) "
        f"and {len(cases)} selftest file(s).",
        flush=True,
    )
    ranked = search(loops, cases)
    passing = [row for row in ranked if row["passed"]]
    show = passing[:5] if passing else ranked[:5]
    if not passing:
        print("No setting kept the sitting file within its step and path limits and the walk within 10%.")
        print("Closest five, which do not pass:")
    else:
        print(f"{len(passing)} settings passed. Best {min(5, len(passing))}:")
    for index, row in enumerate(show, start=1):
        mark = "PASS" if row["passed"] else "FAIL"
        print(f"{index}. {mark}  mean closure {row['mean_closure_m']:.2f} m")
        print(f"   {format_setting(row['setting'])}")
        for name, score in row["cases"].items():
            print(f"   {name}  steps={score['steps']:.2f}  path={score['path_m']:.2f} m")
        for loop in row["loops"]:
            print(f"   {loop['name']}  {loop['label']}  closure {loop['closure_m']:.2f} m")
    if not passing:
        return 1
    best = passing[0]["setting"]
    if not args.apply:
        print("Left ring_config.json unchanged. Pass --apply to write the best setting.")
        return 0
    write_setting(best, CONFIG_PATH)
    print(f"Wrote the best setting to {CONFIG_PATH}")
    return 0


def _draw_loop(ax, tracker: PdrTracker, title: str) -> None:
    colors = {"swing": "#3fb37f", "carry": "#e0b24a", "still": "#88929c"}
    pts = tracker.trace
    for i in range(1, len(pts)):
        ax.plot([pts[i - 1].x, pts[i].x], [pts[i - 1].y, pts[i].y], color=colors.get(pts[i].mode, "#888"), lw=1.4)
    for mark in tracker.marks:
        ax.scatter([mark["x"]], [mark["y"]], c="#e0685e", s=28, zorder=4)
        ax.annotate(mark["label"], (mark["x"], mark["y"]), fontsize=8)
    if pts:
        ax.scatter([pts[-1].x], [pts[-1].y], c="#5aa9f0", s=28, zorder=3)
    ax.set_aspect("equal")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")


def replay_loop(config: Config, loop: dict) -> tuple[PdrTracker, float]:
    """Full tracker replay. Closure is the distance between the two P positions."""
    tracker = PdrTracker(config)
    for sample in loop["rows"]:
        tracker.push(sample)
    points = []
    for index, unix in enumerate(loop["p_unix"], start=1):
        when = unix - loop["t0"]
        chosen = None
        for row in tracker.trace:
            if row.t <= when + 1e-9:
                chosen = row
            else:
                break
        xy = (0.0, 0.0) if chosen is None else (chosen.x, chosen.y)
        tracker.marks.append({"label": f"P{index}", "t": 0.0 if chosen is None else chosen.t, "x": xy[0], "y": xy[1]})
        points.append(xy)
    closure = float(math.hypot(points[0][0] - points[1][0], points[0][1] - points[1][1]))
    return tracker, closure


def run_pdr_report(argv: list[str] | None = None) -> int:
    """Replay every saved loop with the current config and write one side-by-side image."""
    import argparse
    import sys

    from ring.config import load_config
    from ring.service import CONFIG_PATH

    argparse.ArgumentParser(description="Draw every saved PDR loop with the current config.").parse_args(argv)
    loops = load_loops(ROOT)
    if not loops:
        print("No saved loop recordings. Run pdr-loop-test first.", file=sys.stderr)
        return 1
    config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    config.find_backend = "pdr"
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cols = min(len(loops), 4)
    rows = math.ceil(len(loops) / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(4.4 * cols, 4.4 * rows), squeeze=False)
    flat = list(axes.flat)
    for ax, loop in zip(flat, loops):
        tracker, closure = replay_loop(config, loop)
        _draw_loop(ax, tracker, f"{loop['label']}   {closure:.2f} m")
        print(f"{loop['name']}  {loop['label']}  closure {closure:.2f} m")
    for ax in flat[len(loops) :]:
        ax.axis("off")
    fig.tight_layout()
    out = ROOT / "reports" / "pdr_report.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out}")
    return 0
