"""Recalibrate usable_min_sharpness and write contact sheets around the threshold.

    .\\.venv\\Scripts\\python.exe tools\\calibrate_sharpness.py --threshold 8.0
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def contact_sheet(paths: list[Path], out: Path, cols: int = 5, thumb: int = 128) -> None:
    if not paths:
        blank = np.zeros((thumb, thumb, 3), dtype=np.uint8)
        cv2.putText(blank, "none", (20, thumb // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (160, 160, 160), 1)
        cv2.imwrite(str(out), blank)
        return
    images = []
    for path in paths:
        image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            image = np.zeros((thumb, thumb, 3), dtype=np.uint8)
        images.append(cv2.resize(image, (thumb, thumb)))
    cols = max(1, min(cols, len(images)))
    rows = math.ceil(len(images) / cols)
    sheet = np.zeros((rows * thumb, cols * thumb, 3), dtype=np.uint8)
    for index, image in enumerate(images):
        row, col = divmod(index, cols)
        sheet[row * thumb : (row + 1) * thumb, col * thumb : (col + 1) * thumb] = image
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), sheet)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default=str(ROOT / "recordings" / "ring_memory"))
    parser.add_argument("--threshold", type=float, default=8.0)
    parser.add_argument("--out", default=str(ROOT / "reports" / "sharpness_calibration"))
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    data = Path(args.data)
    db = sqlite3.connect(data / "memory.sqlite")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, sharpness, width, height, reject_reason, usable FROM keyframes ORDER BY sharpness"
    ).fetchall()
    if not rows:
        print("No keyframes in", data)
        return 1
    dims = {(r["width"], r["height"]) for r in rows}
    scores = [float(r["sharpness"]) for r in rows]
    threshold = float(args.threshold)
    below = [r for r in rows if r["sharpness"] < threshold]
    above = [r for r in rows if r["sharpness"] >= threshold]
    # Prefer frames closest to the cut from each side.
    below = sorted(below, key=lambda r: threshold - r["sharpness"])[: args.count]
    above = sorted(above, key=lambda r: r["sharpness"] - threshold)[: args.count]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    frames = data / "frames"
    contact_sheet([frames / f"{r['id']}.jpg" for r in below], out / "below_threshold.jpg")
    contact_sheet([frames / f"{r['id']}.jpg" for r in above], out / "above_threshold.jpg")
    summary = {
        "data": str(data),
        "count": len(rows),
        "dims": [list(d) for d in dims],
        "threshold": threshold,
        "sharpness": {
            "min": round(min(scores), 2),
            "p25": round(float(np.percentile(scores, 25)), 2),
            "median": round(float(np.percentile(scores, 50)), 2),
            "p75": round(float(np.percentile(scores, 75)), 2),
            "p90": round(float(np.percentile(scores, 90)), 2),
            "max": round(max(scores), 2),
        },
        "below_count": sum(1 for s in scores if s < threshold),
        "above_count": sum(1 for s in scores if s >= threshold),
        "note": (
            "Laplacian variance is resolution-sensitive. Thresholds tuned on QVGA (320x240) "
            "are not the same as VGA (640x480)."
        ),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print("wrote", out / "below_threshold.jpg")
    print("wrote", out / "above_threshold.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
