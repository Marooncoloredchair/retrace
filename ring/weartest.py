"""Wear-test runner and report writer.

    .\\retrace weartest --minutes 10
"""

from __future__ import annotations

import json
import math
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

from ring.embedder import farthest_point_sample
from ring.store import Store
from ring.vision import lift_brightness_jpeg, mask_finger_jpeg

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    rank = (len(sorted_values) - 1) * (p / 100.0)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return float(sorted_values[low])
    weight = rank - low
    return float(sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight)


def _gap_stats(unix_ms_list: list[int]) -> dict:
    if len(unix_ms_list) < 2:
        return {"median_s": 0.0, "p90_s": 0.0, "max_s": 0.0, "count": 0}
    gaps = sorted((b - a) / 1000.0 for a, b in zip(unix_ms_list, unix_ms_list[1:]))
    return {
        "median_s": round(_percentile(gaps, 50), 3),
        "p90_s": round(_percentile(gaps, 90), 3),
        "max_s": round(gaps[-1], 3),
        "count": len(gaps),
    }


def _contact_sheet(jpeg_paths: list[Path], out_path: Path, cols: int = 8, thumb: int = 160) -> None:
    if not jpeg_paths:
        blank = np.zeros((thumb, thumb, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (20, thumb // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (180, 180, 180), 1)
        cv2.imwrite(str(out_path), blank)
        return
    images = []
    for path in jpeg_paths:
        data = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            image = np.zeros((thumb, thumb, 3), dtype=np.uint8)
        images.append(cv2.resize(image, (thumb, thumb)))
    cols = max(1, min(cols, len(images)))
    rows = math.ceil(len(images) / cols)
    sheet = np.zeros((rows * thumb, cols * thumb, 3), dtype=np.uint8)
    for index, image in enumerate(images):
        row, col = divmod(index, cols)
        sheet[row * thumb : (row + 1) * thumb, col * thumb : (col + 1) * thumb] = image
    cv2.imwrite(str(out_path), sheet)


def pick_near_threshold_spaced(
    rows: list,
    *,
    threshold: float,
    side: str,
    count: int = 20,
    min_gap_ms: int = 2000,
) -> list:
    """Frames just above/below threshold, spread across the session (max one per min_gap_ms)."""
    if side == "below":
        candidates = [r for r in rows if float(r["sharpness"]) < threshold]
        candidates.sort(key=lambda r: threshold - float(r["sharpness"]))
    else:
        candidates = [r for r in rows if float(r["sharpness"]) >= threshold]
        candidates.sort(key=lambda r: float(r["sharpness"]) - threshold)
    picked: list = []
    for row in candidates:
        unix_ms = int(row["unix_ms"])
        if any(abs(unix_ms - int(prev["unix_ms"])) < min_gap_ms for prev in picked):
            continue
        picked.append(row)
        if len(picked) >= count:
            break
    return picked


def _contact_sheet_fullres(
    paths: list[Path],
    scores: list[float],
    out: Path,
    cols: int = 5,
) -> None:
    if not paths:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (160, 160, 160), 2)
        cv2.imwrite(str(out), blank)
        return
    images: list[np.ndarray] = []
    for path, score in zip(paths, scores):
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            image = np.zeros((480, 640, 3), dtype=np.uint8)
        label = f"{score:.2f}"
        cv2.putText(image, label, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 3)
        cv2.putText(image, label, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (40, 255, 120), 2)
        images.append(image)
    h, w = images[0].shape[:2]
    cols = max(1, min(cols, len(images)))
    rows = math.ceil(len(images) / cols)
    sheet = np.zeros((rows * h, cols * w, 3), dtype=np.uint8)
    for index, image in enumerate(images):
        if image.shape[0] != h or image.shape[1] != w:
            image = cv2.resize(image, (w, h))
        row, col = divmod(index, cols)
        sheet[row * h : (row + 1) * h, col * w : (col + 1) * w] = image
    cv2.imwrite(str(out), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 92])


def _in_session(unix_ms: int, started_unix_ms: int | None, ended_unix_ms: int | None) -> bool:
    if started_unix_ms is None or ended_unix_ms is None:
        return True
    return int(started_unix_ms) <= int(unix_ms) <= int(ended_unix_ms)


def match_holds_to_marks(
    detected: list[dict],
    marked: list[dict],
    *,
    pad_ms: int = 2000,
) -> tuple[set, set]:
    """Match if a mark falls inside [grasp-pad, release+pad] of a detected hold."""
    hold_matched: set = set()
    mark_matched: set = set()
    for det in detected:
        grasp = int(det.get("grasp_unix_ms") or det["unix_ms"])
        release = int(det["unix_ms"])
        lo, hi = grasp - pad_ms, release + pad_ms
        for mark in marked:
            mid = int(mark["unix_ms"])
            if mark["id"] in mark_matched:
                continue
            if lo <= mid <= hi:
                hold_matched.add(det["id"])
                mark_matched.add(mark["id"])
                break
    return hold_matched, mark_matched


def write_report(
    store: Store,
    out_dir: Path,
    *,
    minutes: float,
    processed: int,
    usable_rejects: dict[str, int] | None = None,
    started_unix_ms: int | None = None,
    ended_unix_ms: int | None = None,
    sharpness_threshold: float | None = None,
    host_brightness_lift: bool = True,
    host_brightness_target_mean: float = 110.0,
    host_brightness_max_gain: float = 5.0,
    data_dir: str | Path | None = None,
) -> Path:
    """Build reports/weartest_<timestamp>/ from one wear run (session-scoped when bounds given)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    export_dir = out_dir / "export"
    masked_dir = out_dir / "export_masked"
    export_dir.mkdir(exist_ok=True)
    masked_dir.mkdir(exist_ok=True)

    session_filter = started_unix_ms is not None and ended_unix_ms is not None
    params: list = []
    where = ""
    if session_filter:
        where = " WHERE unix_ms >= ? AND unix_ms <= ?"
        params = [int(started_unix_ms), int(ended_unix_ms)]

    with store.lock:
        total = store.db.execute(f"SELECT COUNT(*) AS n FROM keyframes{where}", params).fetchone()["n"]
        if session_filter:
            usable_rows = store.db.execute(
                "SELECT id, unix_ms, finger_fraction, frame_class FROM keyframes "
                "WHERE unix_ms >= ? AND unix_ms <= ? AND usable = 1 ORDER BY unix_ms",
                params,
            ).fetchall()
        else:
            usable_rows = store.db.execute(
                "SELECT id, unix_ms, finger_fraction, frame_class FROM keyframes WHERE usable = 1 ORDER BY unix_ms"
            ).fetchall()
        bank_sql = (
            "SELECT id, unix_ms FROM keyframes WHERE bank_keyframe = 1 AND unix_ms >= ? AND unix_ms <= ? ORDER BY id"
            if session_filter
            else "SELECT id, unix_ms FROM keyframes WHERE bank_keyframe = 1 ORDER BY id"
        )
        bank_rows = store.db.execute(bank_sql, params if session_filter else []).fetchall()
        reject_sql = (
            "SELECT reject_reason AS reason, COUNT(*) AS n FROM keyframes"
            " WHERE usable = 0 AND reject_reason != '' AND unix_ms >= ? AND unix_ms <= ?"
            " GROUP BY reject_reason"
            if session_filter
            else "SELECT reject_reason AS reason, COUNT(*) AS n FROM keyframes"
            " WHERE usable = 0 AND reject_reason != '' GROUP BY reject_reason"
        )
        reject_rows = store.db.execute(reject_sql, params if session_filter else []).fetchall()
        class_sql = (
            "SELECT frame_class AS cls, COUNT(*) AS n FROM keyframes"
            " WHERE frame_class != '' AND unix_ms >= ? AND unix_ms <= ? GROUP BY frame_class"
            if session_filter
            else "SELECT frame_class AS cls, COUNT(*) AS n FROM keyframes"
            " WHERE frame_class != '' GROUP BY frame_class"
        )
        class_rows = store.db.execute(class_sql, params if session_filter else []).fetchall()
        finger_sql = (
            "SELECT finger_fraction FROM keyframes WHERE unix_ms >= ? AND unix_ms <= ?"
            if session_filter
            else "SELECT finger_fraction FROM keyframes"
        )
        finger_vals = [
            row["finger_fraction"]
            for row in store.db.execute(finger_sql, params if session_filter else []).fetchall()
            if row["finger_fraction"] is not None
        ]
        dims_sql = (
            "SELECT width, height, COUNT(*) AS n FROM keyframes"
            " WHERE unix_ms >= ? AND unix_ms <= ? GROUP BY width, height ORDER BY n DESC"
            if session_filter
            else "SELECT width, height, COUNT(*) AS n FROM keyframes GROUP BY width, height ORDER BY n DESC"
        )
        dims = store.db.execute(dims_sql, params if session_filter else []).fetchall()

    usable_ids = [row["id"] for row in usable_rows]
    usable_times = [row["unix_ms"] for row in usable_rows]
    bank_ids = [row["id"] for row in bank_rows]
    reject_counts = {row["reason"]: row["n"] for row in reject_rows}
    analysed_rejects = {key: int(value) for key, value in (usable_rejects or {}).items()}
    class_counts = {row["cls"]: row["n"] for row in class_rows}

    duration_min = max(minutes, 1e-6)
    if session_filter and ended_unix_ms > started_unix_ms:
        duration_min = max((ended_unix_ms - started_unix_ms) / 60_000.0, 1e-6)

    finger_summary = {}
    if finger_vals:
        finger_sorted = sorted(float(v) for v in finger_vals)
        finger_summary = {
            "median": round(_percentile(finger_sorted, 50), 3),
            "p90": round(_percentile(finger_sorted, 90), 3),
            "mean": round(sum(finger_sorted) / len(finger_sorted), 3),
        }

    summary = {
        "total_frames": int(total or 0),
        "processed_frames": int(processed),
        "usable_frames": len(usable_ids),
        "usable_frames_per_minute": round(len(usable_ids) / duration_min, 3),
        "reject_reason_counts": reject_counts,
        "analysed_reject_reason_counts": analysed_rejects,
        "frame_class_counts": class_counts,
        "finger_fraction": finger_summary,
        "frame_sizes": [{"width": d["width"], "height": d["height"], "count": d["n"]} for d in dims],
        "usable_gap_s": _gap_stats(usable_times),
        "keyframes_kept": len(bank_ids),
        "minutes": round(duration_min, 3),
        "report_dir": str(out_dir),
        "session": {
            "started_unix_ms": started_unix_ms,
            "ended_unix_ms": ended_unix_ms,
            "data_dir": str(data_dir or store.root),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Hold detector vs Mark (P) ground truth — primary metrics (session only).
    holds = [
        event
        for event in store.hold_events()
        if _in_session(event["unix_ms"], started_unix_ms, ended_unix_ms)
    ]
    hold_detected = [event for event in holds if event["source"] == "hold"]
    hold_marked = [event for event in holds if event["source"] == "marked"]
    match_window_ms = 2000
    hold_matched, mark_matched = match_holds_to_marks(hold_detected, hold_marked, pad_ms=match_window_ms)
    hold_reports = []
    for event in holds:
        usable_in_window = [
            row
            for row in store.usable_keyframes_between(event["pin_from_unix_ms"], event["pin_to_unix_ms"])
            if _in_session(row["unix_ms"], started_unix_ms, ended_unix_ms)
        ]
        near_marked = event["id"] in hold_matched if event["source"] == "hold" else None
        near_hold = event["id"] in mark_matched if event["source"] == "marked" else None
        hold_reports.append(
            {
                "id": event["id"],
                "unix_ms": event["unix_ms"],
                "source": event["source"],
                "grasp_unix_ms": event.get("grasp_unix_ms"),
                "hold_ms": event.get("hold_ms"),
                "peak_cover": event.get("peak_cover"),
                "usable_frames_in_window": len(usable_in_window),
                "within_2s_of_marked": near_marked,
                "within_2s_of_hold": near_hold,
                "vlm_name": event.get("vlm_name") or "",
                "vlm_description": event.get("vlm_description") or "",
                "vlm_raw": event.get("vlm_raw") or "",
                "vlm_is_none": bool(event.get("vlm_is_none", 1)),
                "vlm_error": event.get("vlm_error") or "",
                "item_id": event.get("item_id"),
            }
        )
    hold_precision = (len(hold_matched) / len(hold_detected)) if hold_detected else None
    hold_recall = (len(mark_matched) / len(hold_marked)) if hold_marked else None

    imu_events = [
        event
        for event in store.putdown_events()
        if _in_session(event["unix_ms"], started_unix_ms, ended_unix_ms)
    ]
    imu_reports = []
    for event in imu_events:
        usable_in_window = store.usable_keyframes_between(event["pin_from_unix_ms"], event["pin_to_unix_ms"])
        imu_reports.append(
            {
                "id": event["id"],
                "unix_ms": event["unix_ms"],
                "source": event["source"],
                "peak_accel_g": event["peak_accel_g"],
                "stillness_ms": event["stillness_ms"],
                "usable_frames_in_window": len(usable_in_window),
            }
        )

    events_payload = {
        "hold_events": hold_reports,
        "hold_precision_near_marked": None if hold_precision is None else round(hold_precision, 3),
        "hold_recall_marked_near_hold": None if hold_recall is None else round(hold_recall, 3),
        "hold_detected_count": len(hold_detected),
        "hold_marked_count": len(hold_marked),
        "match_window_s": match_window_ms / 1000.0,
        "match_rule": "mark inside [grasp-2s, release+2s]",
        "imu_putdown_events": imu_reports,
    }
    (out_dir / "events.json").write_text(json.dumps(events_payload, indent=2), encoding="utf-8")

    bank_paths = [store.frame_dir / f"{kid}.jpg" for kid in bank_ids if (store.frame_dir / f"{kid}.jpg").is_file()]
    _contact_sheet(bank_paths, out_dir / "keyframes_sheet.jpg")

    for event in holds:
        window = [
            row
            for row in store.keyframes_between(event["pin_from_unix_ms"], event["pin_to_unix_ms"])
            if _in_session(row["unix_ms"], started_unix_ms, ended_unix_ms)
        ]
        paths = [store.frame_dir / f"{row['id']}.jpg" for row in window if (store.frame_dir / f"{row['id']}.jpg").is_file()]
        _contact_sheet(paths, out_dir / f"hold_{event['id']}_{event['source']}_sheet.jpg")

    embeddings = []
    embed_ids = []
    for kid in bank_ids:
        vector = store.load_embedding(kid)
        if vector is not None:
            embeddings.append(vector)
            embed_ids.append(kid)
    chosen = farthest_point_sample(embeddings, 30) if embeddings else list(range(min(30, len(bank_ids))))
    export_ids: list[int] = []
    if embeddings:
        export_ids.extend(embed_ids[index] for index in chosen)
    else:
        export_ids.extend(bank_ids[:30])

    for event in holds:
        usable = [
            row
            for row in store.usable_keyframes_between(event["pin_from_unix_ms"], event["pin_to_unix_ms"])
            if _in_session(row["unix_ms"], started_unix_ms, ended_unix_ms)
        ]
        picks = []
        if usable:
            picks.append(usable[0])
            if len(usable) >= 3:
                picks.append(usable[len(usable) // 2])
            if len(usable) >= 2:
                picks.append(usable[-1])
            picks = picks[:3]
        for row in picks:
            if row["id"] not in export_ids:
                export_ids.append(row["id"])

    for index, kid in enumerate(export_ids):
        src = store.frame_dir / f"{kid}.jpg"
        if not src.is_file():
            continue
        jpeg = src.read_bytes()
        if host_brightness_lift:
            jpeg = lift_brightness_jpeg(
                jpeg,
                target_mean=host_brightness_target_mean,
                max_gain=host_brightness_max_gain,
            )
        dest = export_dir / f"{index:03d}_kf{kid}.jpg"
        dest.write_bytes(jpeg)
        (masked_dir / f"{index:03d}_kf{kid}.jpg").write_bytes(mask_finger_jpeg(jpeg))

    sharp_sql = (
        "SELECT id, unix_ms, sharpness FROM keyframes WHERE unix_ms >= ? AND unix_ms <= ? ORDER BY sharpness"
        if session_filter
        else "SELECT id, unix_ms, sharpness FROM keyframes ORDER BY sharpness"
    )
    with store.lock:
        sharp_rows = store.db.execute(sharp_sql, params if session_filter else []).fetchall()
    scores = [float(r["sharpness"]) for r in sharp_rows]
    if sharpness_threshold is None:
        sharpness_threshold = 2.8
    if scores:
        below = pick_near_threshold_spaced(
            sharp_rows, threshold=float(sharpness_threshold), side="below", count=20, min_gap_ms=2000
        )
        above = pick_near_threshold_spaced(
            sharp_rows, threshold=float(sharpness_threshold), side="above", count=20, min_gap_ms=2000
        )

        def _sheet_paths(picked: list) -> list[Path]:
            paths: list[Path] = []
            tmp = out_dir / "_sheet_tmp"
            if host_brightness_lift:
                tmp.mkdir(exist_ok=True)
            for r in picked:
                src = store.frame_dir / f"{r['id']}.jpg"
                if not host_brightness_lift or not src.is_file():
                    paths.append(src)
                    continue
                dest = tmp / f"{r['id']}.jpg"
                dest.write_bytes(
                    lift_brightness_jpeg(
                        src.read_bytes(),
                        target_mean=host_brightness_target_mean,
                        max_gain=host_brightness_max_gain,
                    )
                )
                paths.append(dest)
            return paths

        _contact_sheet_fullres(
            _sheet_paths(below),
            [float(r["sharpness"]) for r in below],
            out_dir / "below_threshold.jpg",
        )
        _contact_sheet_fullres(
            _sheet_paths(above),
            [float(r["sharpness"]) for r in above],
            out_dir / "above_threshold.jpg",
        )
        tmp_dir = out_dir / "_sheet_tmp"
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        summary["sharpness"] = {
            "threshold": float(sharpness_threshold),
            "min": round(min(scores), 3),
            "p10": round(_percentile(sorted(scores), 10), 3),
            "p50": round(_percentile(sorted(scores), 50), 3),
            "p90": round(_percentile(sorted(scores), 90), 3),
            "max": round(max(scores), 3),
            "sheet_below": len(below),
            "sheet_above": len(above),
            "sheet_min_gap_s": 2.0,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    (out_dir / "README.txt").write_text(
        "Wear-test report (session-scoped). summary.json and events.json are the numbers; "
        "keyframes_sheet.jpg and hold_*_sheet.jpg are contact sheets; "
        "above_threshold.jpg / below_threshold.jpg are full-res sharpness sheets "
        "(≤1 frame per 2 s across the session); "
        "export/ holds lifted JPEGs for reconstruction; export_masked/ has fingers blacked out; "
        "memory frames/ keeps original camera JPEGs.\n",
        encoding="utf-8",
    )
    return out_dir


def resolve_data_dir(config, cli_data: str | None = None) -> Path:
    from ring.service import DATA_DIR

    if cli_data:
        return Path(cli_data).expanduser().resolve()
    if getattr(config, "data_dir", ""):
        return Path(config.data_dir).expanduser().resolve()
    return Path(DATA_DIR).resolve()


def run_weartest(argv: list[str] | None = None) -> int:
    """Start the normal pipeline for N minutes, then write the report and exit."""
    import argparse
    import sys
    import threading

    import uvicorn

    from ring.config import load_config
    from ring.service import CONFIG_PATH, build, create_app, port_free, terminal_line

    parser = argparse.ArgumentParser(description="Run a timed ReTrace wear test and write a report.")
    parser.add_argument("--minutes", type=float, default=10.0, help="how long to run (default 10)")
    parser.add_argument("--no-motor", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data", default="", help="override memory folder for this run")
    args = parser.parse_args(argv)

    try:
        config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    except (ValueError, json.JSONDecodeError) as error:
        print(f"ring_config.json has a problem: {error}", file=sys.stderr)
        return 2

    # Calibration wear run: keep all sharpness scores, do not reject for blur.
    config.usable_min_sharpness = 2.8
    config.host_brightness_lift = True

    data_dir = resolve_data_dir(config, args.data or None)
    port = args.port or config.port
    if not port_free(port):
        print(
            f"Port {port} is already in use. Close the other ReTrace window first.",
            file=sys.stderr,
        )
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_dir = REPORTS / f"weartest_{stamp}"
    store, link, engine = build(data_dir, config, allow_motor=not args.no_motor, echo=terminal_line)
    app = create_app(store, link, engine, port)
    url = f"http://127.0.0.1:{port}/"
    started_unix_ms = int(time.time() * 1000)
    print(
        "\n".join(
            [
                "",
                "ReTrace wear test",
                f"  Duration:   {args.minutes:g} minutes",
                f"  Dashboard:  {url}",
                f"  Memory:     {store.root}",
                f"  Report:     {report_dir}",
                "  Mark GT:    dashboard button or press P / Shift+P for Mark put-down",
                "  Stop early: Ctrl+C (report still written)",
                "",
            ]
        ),
        flush=True,
    )

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="weartest-uvicorn")
    if not args.no_browser:
        import webbrowser

        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    thread.start()
    # Hold for the timed window while the normal pipeline runs.
    deadline = time.monotonic() + max(0.1, args.minutes * 60.0)
    try:
        while time.monotonic() < deadline and not server.should_exit:
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nWear test interrupted; writing report…", flush=True)

    ended_unix_ms = int(time.time() * 1000)
    # Drain embedder queue while the store is still open (lifespan closes it on shutdown).
    time.sleep(1.0)
    try:
        write_report(
            store,
            report_dir,
            minutes=args.minutes,
            processed=engine.processed,
            usable_rejects=dict(engine.usable_rejects),
            started_unix_ms=started_unix_ms,
            ended_unix_ms=ended_unix_ms,
            sharpness_threshold=float(config.usable_min_sharpness),
            host_brightness_lift=bool(config.host_brightness_lift),
            host_brightness_target_mean=float(config.host_brightness_target_mean),
            host_brightness_max_gain=float(config.host_brightness_max_gain),
            data_dir=data_dir,
        )
        print(f"Wear test report written to {report_dir}", flush=True)
    except Exception as error:
        print(f"Failed to write wear test report: {type(error).__name__}: {error}", file=sys.stderr)
        server.should_exit = True
        thread.join(timeout=8)
        return 1

    server.should_exit = True
    thread.join(timeout=8)
    return 0
