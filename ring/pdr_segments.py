"""Split a walked loop into walking stretches and the turn between them.

The walking gate decides the stretches. Yaw is the gyro heading. Its mean over
a stretch cancels the arm swing. A turn is that mean's change from one stretch
to the next, or a heading change of at least 60 degrees that holds inside one
stretch. Yaw while the gate is shut (handling the ring) is not a turn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ring.pdr import PdrTracker, world_samples

TURN_MIN_DEG = 60.0
# Each side of an in-stretch turn must hold this long, so one arm swing
# cannot look like a change of heading.
TURN_HOLD_S = 1.5


@dataclass
class Stretch:
    t0: float
    t1: float
    duration_s: float
    steps: float
    mean_yaw: float  # radians
    main: bool

    @property
    def mean_yaw_deg(self) -> float:
        return math.degrees(self.mean_yaw)


@dataclass
class Turn:
    t: float
    degrees: float


@dataclass
class SegmentResult:
    stretches: list[Stretch] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)
    yaw_t: np.ndarray = field(default_factory=lambda: np.zeros(0))
    yaw: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def main_stretches(self) -> list[Stretch]:
        return [stretch for stretch in self.stretches if stretch.main]


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _circular_mean(yaw: np.ndarray) -> float:
    if len(yaw) == 0:
        return 0.0
    return math.atan2(float(np.mean(np.sin(yaw))), float(np.mean(np.cos(yaw))))


def _moving_mean(yaw_unwrapped: np.ndarray, times: np.ndarray, window_s: float) -> np.ndarray:
    out = np.empty_like(yaw_unwrapped)
    left = 0
    acc = 0.0
    for right, t in enumerate(times):
        acc += float(yaw_unwrapped[right])
        while times[left] < t - window_s:
            acc -= float(yaw_unwrapped[left])
            left += 1
        out[right] = acc / (right - left + 1)
    return out


def _walking_runs(trace) -> list[tuple[float, float, float, float]]:
    """Contiguous gate-open runs: (t0, t1, steps_before, steps_after)."""
    runs = []
    current = None
    previous_steps = 0.0
    for row in trace:
        if row.walking and current is None:
            current = [row.t, row.t, previous_steps, row.steps]
        elif row.walking and current is not None:
            current[1] = row.t
            current[3] = row.steps
        elif current is not None:
            runs.append(tuple(current))
            current = None
        previous_steps = row.steps
    if current is not None:
        runs.append(tuple(current))
    return runs


def _steps_at(trace, when: float, default: float) -> float:
    """Cumulative steps on the last hop at or before `when`."""
    found = default
    for row in trace:
        if row.t <= when + 1e-9:
            found = row.steps
        else:
            break
    return found


def _split_run(t0: float, t1: float, steps_before: float, steps_after: float, times: np.ndarray, yaw: np.ndarray, hop_s: float, stop_s: float, trace) -> tuple[list[Stretch], list[Turn]]:
    """One gate-open run, possibly cut where the heading itself turns."""
    mask = (times >= t0 - hop_s) & (times <= t1 + 1e-6)
    sample_t = times[mask]
    sample_yaw = yaw[mask]
    if len(sample_t) < 4:
        duration = max(0.0, t1 - t0 + hop_s)
        stretch = Stretch(t0, t1, duration, steps_after - steps_before, _circular_mean(sample_yaw), duration > stop_s)
        return [stretch], []
    unwrapped = np.unwrap(sample_yaw)
    # Two-second means. One arm-swing cycle is about that long, so the mean
    # is the heading. A turn is a step of 60 degrees or more between
    # successive means that the following two seconds still holds.
    block_s = 2.0
    threshold = math.radians(TURN_MIN_DEG)
    blocks = []
    start = float(sample_t[0])
    end_t = float(sample_t[-1])
    while start + block_s * 0.8 <= end_t:
        stop = min(start + block_s, end_t)
        chosen = (sample_t >= start) & (sample_t < stop)
        if int(np.sum(chosen)) >= 20:
            blocks.append((start, stop, float(np.mean(unwrapped[chosen]))))
        start += block_s
    cuts: list[tuple[float, float]] = []
    for index in range(len(blocks) - 2):
        _b0, boundary, heading = blocks[index]
        _c0, _c1, nxt = blocks[index + 1]
        _d0, _d1, held = blocks[index + 2]
        change = nxt - heading
        if abs(change) < threshold:
            continue
        if abs(held - nxt) > threshold:
            continue
        cuts.append((boundary, blocks[index + 1][0]))
    # Drop a second cut that starts inside the block just used.
    merged: list[tuple[float, float]] = []
    for cut in cuts:
        if merged and cut[0] < merged[-1][1] + block_s:
            continue
        merged.append(cut)
    pieces: list[tuple[float, float]] = []
    cursor_t = float(sample_t[0])
    for cut_start, cut_end in merged:
        if cut_start - cursor_t > 0.2:
            pieces.append((cursor_t, cut_start))
        cursor_t = max(cursor_t, cut_end)
    if float(sample_t[-1]) - cursor_t > 0.2:
        pieces.append((cursor_t, float(sample_t[-1])))
    if not pieces:
        pieces = [(float(sample_t[0]), float(sample_t[-1]))]
    turns: list[Turn] = []
    stretches = []
    cursor = steps_before
    for index, (start, end) in enumerate(pieces):
        piece_yaw = sample_yaw[(sample_t >= start) & (sample_t <= end)]
        duration = max(0.0, float(end - start))
        at_end = _steps_at(trace, end, cursor)
        steps_here = at_end - cursor
        if index < len(pieces) - 1:
            cursor = _steps_at(trace, pieces[index + 1][0], at_end)
        else:
            steps_here = steps_after - cursor
        stretches.append(
            Stretch(
                t0=float(start),
                t1=float(end),
                duration_s=duration,
                steps=max(0.0, steps_here),
                mean_yaw=_circular_mean(piece_yaw),
                main=duration > stop_s,
            )
        )
    for index in range(len(stretches) - 1):
        delta = _wrap(stretches[index + 1].mean_yaw - stretches[index].mean_yaw)
        turns.append(Turn(t=stretches[index].t1, degrees=math.degrees(delta)))
    return stretches, turns


def segment_loop(rows: list[dict], config) -> SegmentResult:
    """Walking stretches and turns for one mark-to-mark loop."""
    tracker = PdrTracker(config)
    for sample in rows:
        tracker.push(sample)
    series = world_samples(rows, config)
    if not series:
        return SegmentResult()
    times = np.array([row[0] for row in series], dtype=np.float64)
    yaw = np.array([row[4] for row in series], dtype=np.float64)
    hop_s = float(config.pdr_hop_s)
    stop_s = float(config.pdr_gait_stop_s)
    stretches: list[Stretch] = []
    turns: list[Turn] = []
    runs = _walking_runs(tracker.trace)
    for t0, t1, steps_before, steps_after in runs:
        parts, inner = _split_run(t0, t1, steps_before, steps_after, times, yaw, hop_s, stop_s, tracker.trace)
        if stretches and parts:
            delta = _wrap(parts[0].mean_yaw - stretches[-1].mean_yaw)
            turns.append(Turn(t=parts[0].t0, degrees=math.degrees(delta)))
        stretches.extend(parts)
        turns.extend(inner)
    return SegmentResult(stretches=stretches, turns=turns, yaw_t=times, yaw=yaw)


def score_loop(result: SegmentResult) -> dict:
    """Two main walks, and the turn between them within 45 degrees of 180."""
    main = result.main_stretches
    two = len(main) == 2
    turn_deg = None
    if two:
        # The turn that sits between the two main stretches, ignoring yaw
        # that happened while the gate was shut.
        between = [
            turn
            for turn in result.turns
            if main[0].t1 - 1e-6 <= turn.t <= main[1].t0 + 1e-6
        ]
        if between:
            # If the heading changed inside the gap and again at the split,
            # the net change between the two means is the turn.
            turn_deg = math.degrees(_wrap(main[1].mean_yaw - main[0].mean_yaw))
        else:
            turn_deg = math.degrees(_wrap(main[1].mean_yaw - main[0].mean_yaw))
    elif len(main) >= 1 and result.turns:
        turn_deg = max(result.turns, key=lambda turn: abs(turn.degrees)).degrees
    # The two checks are separate. With exactly two walks the turn is the
    # difference of their mean yaws. With more walks, score the detected
    # turn closest to 180 degrees.
    if not two and result.turns:
        turn_deg = min(result.turns, key=lambda turn: abs(abs(turn.degrees) - 180.0)).degrees
    turn_ok = turn_deg is not None and abs(abs(turn_deg) - 180.0) <= 45.0
    duration_ratio = None
    step_ratio = None
    if len(main) >= 2:
        outbound, back = main[0], main[1]
        if outbound.duration_s > 0:
            duration_ratio = back.duration_s / outbound.duration_s
        if outbound.steps > 0:
            step_ratio = back.steps / outbound.steps
    return {
        "two_walks": two,
        "turn_ok": turn_ok,
        "turn_deg": turn_deg,
        "both": bool(two and turn_ok),
        "duration_ratio": duration_ratio,
        "step_ratio": step_ratio,
        "main": main,
    }


def format_segments(result: SegmentResult) -> str:
    events = [("walk", stretch.t0, stretch) for stretch in result.stretches]
    events += [("turn", turn.t, turn) for turn in result.turns]
    events.sort(key=lambda item: (item[1], 0 if item[0] == "turn" else 1))
    parts = []
    for kind, _t, value in events:
        if kind == "walk":
            note = "" if value.main else " (short)"
            parts.append(f"walk {value.duration_s:.1f} s / {value.steps:.0f} steps{note}")
        else:
            parts.append(f"turn {value.degrees:+.0f} deg")
    return ", ".join(parts) if parts else "no walking"


def save_figure(path, title: str, result: SegmentResult) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.5, 3.4))
    if len(result.yaw_t):
        unwrapped = np.unwrap(result.yaw)
        degrees = np.degrees(unwrapped - unwrapped[0])
        ax.plot(result.yaw_t, degrees, color="#243044", lw=1.0, label="gyro yaw")
    for stretch in result.stretches:
        color = "#b7e0c2" if stretch.main else "#e7e2c5"
        ax.axvspan(stretch.t0, stretch.t1, color=color, alpha=0.85, lw=0)
    for turn in result.turns:
        ax.axvline(turn.t, color="#c0392b", lw=1.2)
    if len(result.yaw_t):
        top = float(np.max(degrees))
        for turn in result.turns:
            ax.text(turn.t, top, f"  {turn.degrees:+.0f}°", color="#c0392b", fontsize=8, va="top", ha="left")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("gyro yaw (deg)")
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def _spread(values: list[float]) -> str:
    if not values:
        return "none"
    arr = np.array(values, dtype=np.float64)
    return (
        f"min {arr.min():.2f}  median {np.median(arr):.2f}  max {arr.max():.2f}  "
        f"range {arr.max() - arr.min():.2f}"
    )


def run_report(argv: list[str] | None = None) -> int:
    import json
    from pathlib import Path

    from ring.config import load_config
    from ring.pdr_cli import ROOT, _judge, _load_imu_rows, load_case_rows
    from ring.service import CONFIG_PATH

    config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    config.find_backend = "pdr"
    cases = {
        case["name"]: case
        for case in json.loads((ROOT / "reports" / "pdr_selftest" / "cases.json").read_text(encoding="utf-8"))
    }
    from ring.pdr_cli import _run_rows

    print(
        f"gate acf {config.pdr_gait_acf_min:.2f}  band {config.pdr_gait_band_min:.2f}  "
        f"stop {config.pdr_gait_stop_s:.1f} s  swing confidence {config.pdr_swing_confidence:.1f}  "
        f"window {config.pdr_window_s:.1f} s"
    )
    for name in ("sit_hand", "walk30"):
        tracker = _run_rows(config, load_case_rows(cases[name]))
        ok, detail = _judge(tracker, cases[name])
        print(f"selftest {name} {'PASS' if ok else 'FAIL'} {detail}")

    loops = []
    early = ROOT / "reports" / "pdr_import_20261114_140312"
    eight = ROOT / "reports" / "pdr_import_20261114_144215"
    for number, label in ((1, "earlier 1"), (3, "earlier 2")):
        loops.append((label, early / f"loop_{number:02d}" / "imu.csv", f"earlier_{number:02d}"))
    for index, number in enumerate((1, 3, 5, 7, 9, 12, 14, 16), start=1):
        loops.append((f"walk {index}", eight / f"loop_{number:02d}" / "imu.csv", f"walk_{index:02d}"))

    out_dir = ROOT / "reports" / "pdr_segments"
    passed = 0
    duration_ratios = []
    step_ratios = []
    for label, path, slug in loops:
        rows = _load_imu_rows([path])
        result = segment_loop(rows, config)
        scored = score_loop(result)
        if scored["both"]:
            passed += 1
        if scored["duration_ratio"] is not None:
            duration_ratios.append(scored["duration_ratio"])
        if scored["step_ratio"] is not None:
            step_ratios.append(scored["step_ratio"])
        walks = "PASS" if scored["two_walks"] else "FAIL"
        turn = "PASS" if scored["turn_ok"] else "FAIL"
        turn_txt = "none" if scored["turn_deg"] is None else f"{scored['turn_deg']:+.0f} deg"
        dur = "n/a" if scored["duration_ratio"] is None else f"{scored['duration_ratio']:.2f}"
        steps = "n/a" if scored["step_ratio"] is None else f"{scored['step_ratio']:.2f}"
        print(f"{label}: {format_segments(result)}")
        print(f"   two walks {walks}  turn {turn} ({turn_txt})  duration ratio {dur}  step ratio {steps}")
        save_figure(out_dir / f"{slug}.png", f"{label}  {format_segments(result)}", result)
    print(f"both checks: {passed} of {len(loops)}")
    print(f"duration ratio  {_spread(duration_ratios)}")
    print(f"step ratio      {_spread(step_ratios)}")
    print(f"figures in {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_report())
