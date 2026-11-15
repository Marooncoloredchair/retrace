"""Capture worn footage for JPEG/AE/sharpness quality check (not a full weartest).

    .\\.venv\\Scripts\\python.exe tools\\quality_check.py --seconds 120 --threshold 5.0
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def contact_sheet_fullres(paths: list[Path], scores: list[float], out: Path, cols: int = 5) -> None:
    """Tile frames at native resolution with score labels."""
    if not paths:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (160, 160, 160), 2)
        out.parent.mkdir(parents=True, exist_ok=True)
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
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), sheet, [int(cv2.IMWRITE_JPEG_QUALITY), 92])


def mean_brightness(path: Path) -> float:
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return float("nan")
    return float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).mean())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--threshold", type=float, default=5.0)
    parser.add_argument("--out", default=str(ROOT / "reports" / "quality_check"))
    parser.add_argument("--data", default=str(ROOT / "recordings" / "quality_check_run"))
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--skip-record", action="store_true")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data = Path(args.data)

    if not args.skip_record:
        if data.exists():
            shutil.rmtree(data)
        data.mkdir(parents=True, exist_ok=True)
        from ring.config import Config
        from ring.service import build, create_app, port_free
        import threading
        import uvicorn

        port = 8012
        if not port_free(port):
            print(f"port {port} busy")
            return 2
        config = Config(
            embedder_enabled=False,
            vlm_provider="none",
            port=port,
            process_fps=12.0,
            observation_gap_s=0.15,
            usable_min_sharpness=0.0,  # keep blurry frames for score sheets
        )
        store, link, engine = build(data, config, allow_motor=False)
        app = create_app(store, link, engine, port)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        print(f"Recording {args.seconds:.0f}s worn footage → {data}", flush=True)
        print("Wear the ring and look around indoors for the full window.", flush=True)
        time.sleep(float(args.seconds))
        link_status = link.status()
        stats = {
            "processed": engine.processed,
            "usable": engine.usable_count,
            "usable_rejects": dict(engine.usable_rejects),
            "camera_fps": link_status.get("camera_fps"),
            "frames_valid": link_status.get("frames_valid"),
            "frames_invalid": link_status.get("frames_invalid"),
            "jpeg_bytes_mean": link_status.get("jpeg_bytes_mean"),
        }
        server.should_exit = True
        thread.join(timeout=8)
        try:
            store.close()
        except Exception:
            pass
        print("engine stats:", json.dumps(stats, indent=2), flush=True)
    else:
        stats = {}

    db = sqlite3.connect(data / "memory.sqlite")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, sharpness, width, height, usable, reject_reason, bytes, brightness, "
        "occlusion, finger_fraction, dark_fraction FROM keyframes ORDER BY sharpness"
    ).fetchall()
    if not rows:
        print("No keyframes in", data)
        return 1

    scores = np.array([float(r["sharpness"]) for r in rows], dtype=np.float64)
    jpeg_sizes = [int(r["bytes"] or 0) for r in rows]
    frames_dir = data / "frames"
    # Prefer stored brightness; fall back to decoding a subsample.
    brightness = [float(r["brightness"]) for r in rows if r["brightness"] is not None]
    if not brightness:
        sample_idx = np.linspace(0, len(rows) - 1, num=min(40, len(rows)), dtype=int)
        brightness = [mean_brightness(frames_dir / f"{rows[i]['id']}.jpg") for i in sample_idx]
        brightness = [b for b in brightness if not math.isnan(b)]

    threshold = float(args.threshold)
    # Prefer unix_ms-ordered rows for spaced sampling across the session.
    from ring.weartest import pick_near_threshold_spaced

    timed = db.execute(
        "SELECT id, unix_ms, sharpness, width, height, usable, reject_reason, bytes, brightness, "
        "occlusion, finger_fraction, dark_fraction FROM keyframes ORDER BY unix_ms"
    ).fetchall()
    below_rows = pick_near_threshold_spaced(
        timed, threshold=threshold, side="below", count=args.count, min_gap_ms=2000
    )
    above_rows = pick_near_threshold_spaced(
        timed, threshold=threshold, side="above", count=args.count, min_gap_ms=2000
    )

    contact_sheet_fullres(
        [frames_dir / f"{r['id']}.jpg" for r in below_rows],
        [float(r["sharpness"]) for r in below_rows],
        out / "below_threshold.jpg",
    )
    contact_sheet_fullres(
        [frames_dir / f"{r['id']}.jpg" for r in above_rows],
        [float(r["sharpness"]) for r in above_rows],
        out / "above_threshold.jpg",
    )

    usable = [r for r in rows if int(r["usable"]) == 1]
    if not usable:
        # Fall back to low-cover frames if threshold rejected everything.
        usable = [
            r
            for r in rows
            if (r["reject_reason"] or "") in ("", "too_blurry", None)
            and max(float(r["finger_fraction"] or 0), float(r["dark_fraction"] or 0)) <= 0.25
        ]
    usable = sorted(usable, key=lambda r: int(r["id"]))
    sample_paths: list[str] = []
    if usable:
        picks = np.linspace(0, len(usable) - 1, num=min(args.samples, len(usable)), dtype=int)
        samples_dir = out / "samples"
        if samples_dir.exists():
            shutil.rmtree(samples_dir)
        samples_dir.mkdir(parents=True, exist_ok=True)
        for n, index in enumerate(picks, start=1):
            row = usable[int(index)]
            src = frames_dir / f"{row['id']}.jpg"
            dst = samples_dir / f"sample_{n:02d}_id{row['id']}_s{float(row['sharpness']):.2f}.jpg"
            shutil.copy2(src, dst)
            sample_paths.append(str(dst.resolve().relative_to(ROOT)))

    summary = {
        "data": str(data),
        "out": str(out),
        "seconds": args.seconds,
        "threshold": threshold,
        "engine_stats": stats,
        "frame_count": len(rows),
        "dims": sorted({(int(r["width"]), int(r["height"])) for r in rows}),
        "sharpness": {
            "min": round(float(scores.min()), 3),
            "p10": round(float(np.percentile(scores, 10)), 3),
            "p50": round(float(np.percentile(scores, 50)), 3),
            "p90": round(float(np.percentile(scores, 90)), 3),
            "max": round(float(scores.max()), 3),
        },
        "jpeg_bytes": {
            "min": min(jpeg_sizes) if jpeg_sizes else 0,
            "mean": int(round(float(np.mean(jpeg_sizes)))) if jpeg_sizes else 0,
            "max": max(jpeg_sizes) if jpeg_sizes else 0,
        },
        "mean_brightness_sampled": {
            "min": round(min(brightness), 1) if brightness else None,
            "mean": round(float(np.mean(brightness)), 1) if brightness else None,
            "max": round(max(brightness), 1) if brightness else None,
            "n": len(brightness),
        },
        "below_count": len(below_rows),
        "above_count": len(above_rows),
        "usable_count": sum(1 for r in rows if int(r["usable"]) == 1),
        "reject_reasons": {},
        "samples": sample_paths,
        "sheets": {
            "above": str((out / "above_threshold.jpg").resolve().relative_to(ROOT)),
            "below": str((out / "below_threshold.jpg").resolve().relative_to(ROOT)),
        },
    }
    reasons: dict[str, int] = {}
    for r in rows:
        key = r["reject_reason"] or ("usable" if int(r["usable"]) else "other")
        reasons[key] = reasons.get(key, 0) + 1
    summary["reject_reasons"] = reasons
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
