"""Hold diagnostics and offline replay for weartest runs.

    .\\retrace replay-holds reports\\weartest_20261114_045012
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from ring.config import Config
from ring.hold import HoldDetector
from ring.store import Store
from ring.vision import lift_brightness_jpeg
from ring.vlm import call_vlm, resolve_vlm_provider
from ring.weartest import match_holds_to_marks

ROOT = Path(__file__).resolve().parents[1]


def _gyro_from_imu(imu) -> float:
    if not imu:
        return float("nan")
    if isinstance(imu, str):
        try:
            imu = json.loads(imu)
        except json.JSONDecodeError:
            return float("nan")
    at = imu.get("at_capture") if isinstance(imu, dict) else None
    if not at:
        return float("nan")
    try:
        gx = float(at.get("gx_dps", 0.0))
        gy = float(at.get("gy_dps", 0.0))
        gz = float(at.get("gz_dps", 0.0))
    except (TypeError, ValueError):
        return float("nan")
    return math.sqrt(gx * gx + gy * gy + gz * gz)


def session_bounds_from_report(run_dir: Path, store: Store) -> tuple[int, int]:
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    session = summary.get("session") or {}
    started = session.get("started_unix_ms")
    ended = session.get("ended_unix_ms")
    if started is not None and ended is not None:
        return int(started), int(ended)
    minutes = float(summary.get("minutes") or 10.0)
    with store.lock:
        row = store.db.execute("SELECT MAX(unix_ms) AS b FROM keyframes WHERE width >= 640").fetchone()
        if row is None or row["b"] is None:
            row = store.db.execute("SELECT MAX(unix_ms) AS b FROM keyframes").fetchone()
    ended_ms = int(row["b"])
    started_ms = ended_ms - int(minutes * 60_000)
    return started_ms, ended_ms


def resolve_data_dir(run_dir: Path, cli_data: str | None = None) -> Path:
    if cli_data:
        return Path(cli_data).expanduser().resolve()
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        session = summary.get("session") or {}
        if session.get("data_dir"):
            return Path(session["data_dir"]).expanduser().resolve()
    return (ROOT / "recordings" / "ring_memory").resolve()


def load_session_series(store: Store, started: int, ended: int) -> list[dict]:
    with store.lock:
        rows = store.db.execute(
            "SELECT id, unix_ms, finger_fraction, dark_fraction, sharpness, imu, brightness "
            "FROM keyframes WHERE unix_ms >= ? AND unix_ms <= ? ORDER BY unix_ms",
            (started, ended),
        ).fetchall()
    series = []
    for row in rows:
        finger = float(row["finger_fraction"] or 0.0)
        dark = float(row["dark_fraction"] or 0.0)
        series.append(
            {
                "id": int(row["id"]),
                "unix_ms": int(row["unix_ms"]),
                "finger_fraction": finger,
                "dark_fraction": dark,
                "cover": max(finger, dark),
                "sharpness": float(row["sharpness"] or 0.0),
                "brightness": float(row["brightness"] or 0.0),
                "gyro_dps": _gyro_from_imu(row["imu"]),
            }
        )
    return series


def write_hold_debug_plot(
    out_path: Path,
    series: list[dict],
    marked: list[dict],
    detected: list[dict],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not series:
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.set_title("no session frames")
        fig.savefig(out_path, dpi=140)
        plt.close(fig)
        return

    t0 = series[0]["unix_ms"]
    times = [(row["unix_ms"] - t0) / 1000.0 for row in series]
    cover = [row["cover"] for row in series]
    finger = [row["finger_fraction"] for row in series]
    gyro = [row["gyro_dps"] if not math.isnan(row["gyro_dps"]) else 0.0 for row in series]

    fig, axes = plt.subplots(3, 1, figsize=(14, 8), sharex=True, gridspec_kw={"hspace": 0.08})
    axes[0].plot(times, cover, color="#1f77b4", lw=1.0, label="cover")
    axes[0].set_ylabel("cover")
    axes[0].set_ylim(0, 1.05)
    axes[0].legend(loc="upper right")
    axes[1].plot(times, finger, color="#ff7f0e", lw=1.0, label="finger")
    if series and "defocus" in series[0]:
        axes[1].plot(times, [row.get("defocus", 0) for row in series], color="#8c564b", lw=0.8, label="defocus")
        axes[1].plot(times, [row.get("grid_dark", 0) for row in series], color="#7f7f7f", lw=0.8, label="dark")
        axes[1].plot(times, [row.get("skin", 0) for row in series], color="#e377c2", lw=0.8, label="skin")
    axes[1].set_ylabel("cover parts")
    axes[1].set_ylim(0, 1.05)
    axes[1].legend(loc="upper right")
    axes[2].plot(times, gyro, color="#2ca02c", lw=1.0, label="gyro dps")
    axes[2].set_ylabel("gyro")
    axes[2].set_xlabel("seconds from session start")
    axes[2].legend(loc="upper right")

    for mark in marked:
        x = (int(mark["unix_ms"]) - t0) / 1000.0
        for ax in axes:
            ax.axvline(x, color="#d62728", lw=1.4, alpha=0.9)
    for det in detected:
        grasp = int(det.get("grasp_unix_ms") or det["unix_ms"])
        release = int(det["unix_ms"])
        x0 = (grasp - t0) / 1000.0
        x1 = (release - t0) / 1000.0
        for ax in axes:
            ax.axvspan(x0, x1, color="#9467bd", alpha=0.22)

    axes[0].set_title(
        f"hold debug  marks={len(marked)} (red lines)  holds={len(detected)} (purple spans)"
    )
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def write_event_contact_sheet(
    store: Store,
    center_unix_ms: int,
    out_path: Path,
    *,
    before_s: float = 4.0,
    after_s: float = 4.0,
    step_s: float = 0.3,
    lift: bool = True,
) -> int:
    """Frames from before_s..after_s around center, one per step_s, timestamp labeled."""
    lo = center_unix_ms - int(before_s * 1000)
    hi = center_unix_ms + int(after_s * 1000)
    rows = store.keyframes_between(lo, hi)
    if not rows:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (160, 160, 160), 2)
        cv2.imwrite(str(out_path), blank)
        return 0

    # Greedy pick nearest frame to each step slot.
    slots = list(np.arange(lo, hi + 1, int(step_s * 1000)))
    picked = []
    used: set[int] = set()
    for slot in slots:
        best = None
        best_dist = 10**18
        for row in rows:
            rid = int(row["id"])
            if rid in used:
                continue
            dist = abs(int(row["unix_ms"]) - int(slot))
            if dist < best_dist:
                best = row
                best_dist = dist
        if best is None or best_dist > int(step_s * 1000):
            continue
        used.add(int(best["id"]))
        picked.append(best)

    thumbs = []
    for row in picked:
        src = store.frame_dir / f"{row['id']}.jpg"
        if not src.is_file():
            continue
        jpeg = src.read_bytes()
        if lift:
            jpeg = lift_brightness_jpeg(jpeg)
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            continue
        image = cv2.resize(image, (160, 120))
        stamp = datetime.fromtimestamp(int(row["unix_ms"]) / 1000.0).strftime("%H:%M:%S.%f")[:-3]
        rel = (int(row["unix_ms"]) - center_unix_ms) / 1000.0
        label = f"{stamp}  {rel:+.1f}s"
        cv2.putText(image, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 2)
        cv2.putText(image, label, (4, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (40, 255, 120), 1)
        thumbs.append(image)

    if not thumbs:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (160, 160, 160), 2)
        cv2.imwrite(str(out_path), blank)
        return 0

    cols = min(8, len(thumbs))
    rows_n = math.ceil(len(thumbs) / cols)
    h, w = thumbs[0].shape[:2]
    sheet = np.zeros((rows_n * h, cols * w, 3), dtype=np.uint8)
    for index, image in enumerate(thumbs):
        r, c = divmod(index, cols)
        sheet[r * h : (r + 1) * h, c * w : (c + 1) * w] = image
    cv2.imwrite(str(out_path), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return len(thumbs)


def write_diagnostics_for_run(run_dir: Path, data_dir: Path | None = None) -> dict:
    """Task-2 diagnostics from the events already recorded in the report / store."""
    run_dir = Path(run_dir)
    data = resolve_data_dir(run_dir, str(data_dir) if data_dir else None)
    store = Store(data)
    started, ended = session_bounds_from_report(run_dir, store)
    series = load_session_series(store, started, ended)

    events_path = run_dir / "events.json"
    events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.is_file() else {}
    holds = [
        e
        for e in events.get("hold_events", [])
        if started <= int(e["unix_ms"]) <= ended
    ]
    marked = [e for e in holds if e["source"] == "marked"]
    detected = [e for e in holds if e["source"] == "hold"]

    write_hold_debug_plot(run_dir / "hold_debug.png", series, marked, detected)
    sheets = []
    for event in marked:
        path = run_dir / f"diag_marked_{event['id']}_sheet.jpg"
        n = write_event_contact_sheet(store, int(event["unix_ms"]), path)
        sheets.append({"kind": "marked", "id": event["id"], "path": str(path.name), "frames": n})
    for event in detected:
        center = int(event.get("grasp_unix_ms") or event["unix_ms"])
        # Center sheet on mid-hold when possible.
        release = int(event["unix_ms"])
        mid = (center + release) // 2
        path = run_dir / f"diag_hold_{event['id']}_sheet.jpg"
        n = write_event_contact_sheet(store, mid, path)
        sheets.append({"kind": "hold", "id": event["id"], "path": str(path.name), "frames": n})

    payload = {
        "run_dir": str(run_dir),
        "data_dir": str(data),
        "session": {"started_unix_ms": started, "ended_unix_ms": ended},
        "series_frames": len(series),
        "marked": len(marked),
        "detected": len(detected),
        "hold_debug": "hold_debug.png",
        "sheets": sheets,
    }
    (run_dir / "hold_debug.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    store.close()
    return payload


def _pick_vlm_jpegs(store: Store, grasp_ms: int, release_ms: int, config: Config) -> list[bytes]:
    pre = store.keyframes_between(grasp_ms - int(config.hold_pre_grasp_s * 1000), grasp_ms)
    mid = store.keyframes_between(grasp_ms + 1, release_ms - 1)
    post = store.keyframes_between(release_ms, release_ms + int(config.hold_post_release_s * 1000))

    def top(rows, n):
        rows = sorted(rows, key=lambda r: float(r["sharpness"] or 0), reverse=True)[:n]
        out = []
        for row in rows:
            jpeg = store.jpeg(row["id"])
            if jpeg:
                out.append(jpeg)
        return out

    def spaced(rows, n):
        if not rows or n <= 0:
            return []
        rows = sorted(rows, key=lambda r: int(r["unix_ms"]))
        if len(rows) <= n:
            chosen = rows
        else:
            idxs = [int(round(i * (len(rows) - 1) / (n - 1))) for i in range(n)]
            chosen = [rows[i] for i in idxs]
        out = []
        for row in chosen:
            jpeg = store.jpeg(row["id"])
            if jpeg:
                out.append(jpeg)
        return out

    jpegs = top(pre, config.hold_pre_frames) + spaced(mid, config.hold_mid_frames) + top(post, config.hold_post_frames)
    if config.host_brightness_lift:
        jpegs = [
            lift_brightness_jpeg(
                j,
                target_mean=config.host_brightness_target_mean,
                max_gain=config.host_brightness_max_gain,
            )
            for j in jpegs
        ]
    return jpegs


def replay_holds(run_dir: Path, data_dir: Path | None = None, *, call_model: bool = True) -> dict:
    run_dir = Path(run_dir)
    data = resolve_data_dir(run_dir, str(data_dir) if data_dir else None)
    config = Config()
    provider, model, warning = resolve_vlm_provider(config.vlm_provider, config.vlm_model)
    if warning:
        print(warning, flush=True)

    store = Store(data)
    started, ended = session_bounds_from_report(run_dir, store)
    series = load_session_series(store, started, ended)

    # Ground-truth marks from the original report (session-scoped).
    events_path = run_dir / "events.json"
    events = json.loads(events_path.read_text(encoding="utf-8")) if events_path.is_file() else {}
    marked = [
        e
        for e in events.get("hold_events", [])
        if e.get("source") == "marked" and started <= int(e["unix_ms"]) <= ended
    ]

    detector = HoldDetector(config)
    detected: list[dict] = []
    t0_mono = time.monotonic()
    t0_unix = series[0]["unix_ms"] if series else started
    for row in series:
        now = t0_mono + (row["unix_ms"] - t0_unix) / 1000.0
        jpeg = store.jpeg(row["id"])
        if jpeg:
            image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                from ring.vision import grid_cover_measures

                measured = grid_cover_measures(image)
                row["defocus"] = measured["defocus"]
                row["grid_dark"] = measured["dark"]
                row["skin"] = measured["skin"]
                row["cover"] = measured["cover"]
        gyro = row.get("gyro_dps")
        event = detector.update(row["cover"], now, row["unix_ms"], None if gyro != gyro else gyro)
        if event is None:
            continue
        detected.append(
            {
                "id": len(detected) + 1,
                "unix_ms": event.unix_ms,
                "source": "hold",
                "grasp_unix_ms": event.grasp_unix_ms,
                "hold_ms": event.hold_ms,
                "peak_cover": event.peak_cover,
                "baseline": event.baseline,
            }
        )

    # VLM naming
    for det in detected:
        jpegs = _pick_vlm_jpegs(store, int(det["grasp_unix_ms"]), int(det["unix_ms"]), config)
        if call_model and provider != "none" and jpegs:
            answer = call_vlm(provider, jpegs, model)
        else:
            from ring.vlm import VlmAnswer

            if not call_model:
                err = "vlm skipped (--no-vlm)"
            elif provider == "none":
                err = "vlm disabled"
            else:
                err = "no frames"
            answer = VlmAnswer(
                raw="",
                name="",
                description="",
                is_none=True,
                error=err,
                put_down=False,
            )
        det["vlm"] = {
            "raw": answer.raw,
            "put_down": answer.put_down,
            "object": answer.name,
            "place": answer.place or answer.description,
            "is_none": answer.is_none,
            "error": answer.error,
            "jpeg_count": len(jpegs),
        }

    hold_matched, mark_matched = match_holds_to_marks(detected, marked, pad_ms=2000)
    precision = (len(hold_matched) / len(detected)) if detected else None
    recall = (len(mark_matched) / len(marked)) if marked else None

    write_hold_debug_plot(run_dir / "hold_debug_replay.png", series, marked, detected)

    payload = {
        "run_dir": str(run_dir),
        "data_dir": str(data),
        "session": {"started_unix_ms": started, "ended_unix_ms": ended, "frames": len(series)},
        "vlm_provider": provider,
        "vlm_model": model,
        "vlm_warning": warning,
        "marked_count": len(marked),
        "detected_count": len(detected),
        "precision": None if precision is None else round(precision, 3),
        "recall": None if recall is None else round(recall, 3),
        "match_rule": "mark inside [grasp-2s, release+2s]",
        "marked": marked,
        "detected": detected,
        "hold_matched_ids": sorted(hold_matched),
        "mark_matched_ids": sorted(mark_matched),
    }
    (run_dir / "replay_holds.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    store.close()
    return payload


def run_vlm_test(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Send one image to the configured VLM and print the reply.")
    parser.add_argument("image", help="path to a JPEG/PNG")
    parser.add_argument("--provider", default="auto")
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)

    path = Path(args.image)
    if not path.is_file():
        print(f"Image not found: {path}", file=sys.stderr)
        return 2
    provider, model, warning = resolve_vlm_provider(args.provider, args.model)
    if warning:
        print(warning, flush=True)
    if provider == "none":
        return 2
    jpeg = path.read_bytes()
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            print("Could not decode image", file=sys.stderr)
            return 2
        ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        if not ok:
            print("Could not encode JPEG", file=sys.stderr)
            return 2
        jpeg = buf.tobytes()
    print(f"Calling {provider} model={model or 'default'} with {path} ({len(jpeg)} bytes)…", flush=True)
    answer = call_vlm(provider, [jpeg], model)
    print("--- raw ---")
    print(answer.raw or "(empty)")
    print("--- parsed ---")
    print(
        json.dumps(
            {
                "put_down": answer.put_down,
                "object": answer.name,
                "place": answer.place or answer.description,
                "is_none": answer.is_none,
                "error": answer.error,
            },
            indent=2,
        )
    )
    return 0 if not answer.error else 1


def run_replay_holds(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Replay hold detection + VLM on a stored weartest run.")
    parser.add_argument("run_dir", help="reports/weartest_… folder")
    parser.add_argument("--data", default="", help="override recordings folder")
    parser.add_argument("--no-vlm", action="store_true", help="skip model calls")
    parser.add_argument("--diagnostics-only", action="store_true", help="only write hold_debug + sheets")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        # Allow bare name under reports/
        alt = ROOT / "reports" / args.run_dir
        if alt.is_dir():
            run_dir = alt
        else:
            print(f"Run dir not found: {args.run_dir}", file=sys.stderr)
            return 2

    data = Path(args.data).expanduser().resolve() if args.data else None
    if args.diagnostics_only:
        payload = write_diagnostics_for_run(run_dir, data)
        print(json.dumps(payload, indent=2))
        return 0

    # Always refresh diagnostics for the recorded events first.
    write_diagnostics_for_run(run_dir, data)
    payload = replay_holds(run_dir, data, call_model=not args.no_vlm)
    print(json.dumps({k: payload[k] for k in (
        "run_dir", "session", "vlm_provider", "vlm_model", "marked_count", "detected_count",
        "precision", "recall", "hold_matched_ids", "mark_matched_ids",
    )}, indent=2))
    print("--- detected ---")
    for det in payload["detected"]:
        vlm = det.get("vlm") or {}
        print(
            f"  hold#{det['id']} release={det['unix_ms']} grasp={det['grasp_unix_ms']} "
            f"peak={det['peak_cover']} baseline={det.get('baseline')} "
            f"vlm={vlm.get('put_down')} object={vlm.get('object')!r} place={vlm.get('place')!r} "
            f"err={vlm.get('error')!r}"
        )
    print("--- marks ---")
    for mark in payload["marked"]:
        print(f"  mark#{mark['id']} unix_ms={mark['unix_ms']}")
    print(f"Wrote {run_dir / 'replay_holds.json'}")
    return 0
