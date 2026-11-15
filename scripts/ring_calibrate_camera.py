"""Measure the ring camera's lens from a recording of a chessboard. Reads files only.

    .\\.venv\\Scripts\\python.exe scripts\\ring_calibrate_camera.py                     (newest recording)
    .\\.venv\\Scripts\\python.exe scripts\\ring_calibrate_camera.py <recording folder>
    .\\.venv\\Scripts\\python.exe scripts\\ring_calibrate_camera.py --selftest          (no camera needed)

ORB-SLAM3, and any other method that turns pictures into geometry, needs the
camera's focal length, optical centre and lens distortion. Those numbers are
measured here from real pictures of a flat chessboard. They are never guessed.

How to make the recording:
  1. Show a chessboard with 9 by 6 inner corners (10 by 7 squares) full-screen on
     a monitor, or print one and tape it flat.
  2. Take the ring off your finger so nothing covers the lens.
  3. Run scripts\\ring_record.py --seconds 40 --label chessboard
  4. Move the camera slowly so the board appears in every part of the picture,
     near and far, straight on and tilted. Keep the whole board in view.

The result is written to calibration/ring_camera.json only when it passes the
checks printed at the end. It is valid for this lens at this picture size only.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FOOTAGE_DIR = ROOT / "recordings" / "ring_footage"
CALIBRATION_FILE = ROOT / "calibration" / "ring_camera.json"

MIN_VIEWS = 12
MAX_RMS_PX = 1.0
MIN_COVERAGE = 0.5
MIN_MOVE_PX = 8.0
GRID_COLS, GRID_ROWS = 8, 6


def newest_recording() -> Path:
    folders = sorted(path for path in FOOTAGE_DIR.glob("*") if (path / "frames.csv").is_file())
    if not folders:
        raise SystemExit(f"No recording found under {FOOTAGE_DIR}. Run scripts\\ring_record.py first.")
    return folders[-1]


def detect(images, cols: int, rows: int) -> tuple[list[np.ndarray], tuple[int, int], int, int]:
    """Find the board in each picture and keep views that differ from the last kept one."""
    kept: list[np.ndarray] = []
    size = (0, 0)
    examined = found = 0
    for image in images:
        if image is None:
            continue
        examined += 1
        size = (image.shape[1], image.shape[0])
        ok, corners = cv2.findChessboardCornersSB(image, (cols, rows), flags=cv2.CALIB_CB_NORMALIZE_IMAGE)
        if not ok:
            continue
        found += 1
        corners = corners.reshape(-1, 2).astype(np.float32)
        if kept and float(np.mean(np.linalg.norm(corners - kept[-1], axis=1))) < MIN_MOVE_PX:
            continue
        kept.append(corners)
    return kept, size, examined, found


def calibrate(views: list[np.ndarray], size: tuple[int, int], cols: int, rows: int, max_views: int) -> dict:
    if len(views) > max_views:
        views = [views[index] for index in np.linspace(0, len(views) - 1, max_views).astype(int)]
    board = np.zeros((cols * rows, 3), np.float32)
    board[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2)
    object_points = [board] * len(views)
    image_points = [view.reshape(-1, 1, 2) for view in views]
    rms, matrix, distortion, _rvecs, _tvecs = cv2.calibrateCamera(
        object_points, image_points, size, None, None, flags=cv2.CALIB_FIX_K3
    )
    width, height = size
    cells = np.zeros((GRID_ROWS, GRID_COLS), dtype=bool)
    for view in views:
        for x, y in view:
            cells[min(GRID_ROWS - 1, max(0, int(y * GRID_ROWS / height))), min(GRID_COLS - 1, max(0, int(x * GRID_COLS / width)))] = True
    k1, k2, p1, p2 = (float(value) for value in distortion.ravel()[:4])
    return {
        "width": width,
        "height": height,
        "fx": float(matrix[0, 0]),
        "fy": float(matrix[1, 1]),
        "cx": float(matrix[0, 2]),
        "cy": float(matrix[1, 2]),
        "k1": k1,
        "k2": k2,
        "p1": p1,
        "p2": p2,
        "rms_px": float(rms),
        "views": len(views),
        "corner_coverage": float(cells.mean()),
        "coverage_map": ["".join("#" if cell else "." for cell in row) for row in cells],
    }


def problems(result: dict) -> list[str]:
    found = []
    if result["views"] < MIN_VIEWS:
        found.append(f"only {result['views']} different views of the board; at least {MIN_VIEWS} are needed")
    if result["rms_px"] > MAX_RMS_PX:
        found.append(f"fit error {result['rms_px']:.2f} pixels is above {MAX_RMS_PX:.1f}")
    if result["corner_coverage"] < MIN_COVERAGE:
        found.append(
            f"the board only appeared in {100 * result['corner_coverage']:.0f}% of the picture area; "
            f"at least {100 * MIN_COVERAGE:.0f}% is needed, or the lens distortion is a guess where it never appeared"
        )
    if not 0.9 < result["fx"] / result["fy"] < 1.1:
        found.append(f"fx {result['fx']:.1f} and fy {result['fy']:.1f} differ by more than 10%, which a normal lens does not do")
    if not (0.25 * result["width"] < result["cx"] < 0.75 * result["width"] and 0.25 * result["height"] < result["cy"] < 0.75 * result["height"]):
        found.append(f"optical centre ({result['cx']:.0f}, {result['cy']:.0f}) is far from the middle of the picture")
    return found


def describe(result: dict) -> str:
    lines = [
        f"  Picture size {result['width']}x{result['height']}. Views used: {result['views']}.",
        f"  Focal length fx {result['fx']:.2f}, fy {result['fy']:.2f} pixels. Optical centre cx {result['cx']:.2f}, cy {result['cy']:.2f}.",
        f"  Distortion k1 {result['k1']:.5f}, k2 {result['k2']:.5f}, p1 {result['p1']:.5f}, p2 {result['p2']:.5f}.",
        f"  Fit error (RMS reprojection): {result['rms_px']:.3f} pixels.",
        f"  Picture area where board corners appeared: {100 * result['corner_coverage']:.0f}%  ('#' seen, '.' never seen)",
    ]
    lines += ["    " + row for row in result["coverage_map"]]
    return "\n".join(lines)


def recording_images(folder: Path, every: int):
    with (folder / "frames.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows[::every]:
        yield cv2.imread(str(folder / row["file"]), cv2.IMREAD_GRAYSCALE)


def synthetic_views(cols: int, rows: int, count: int, truth: dict):
    """Render a chessboard through a known lens. Used only by --selftest."""
    rng = np.random.default_rng(11)
    square = 60
    texture = np.full(((rows + 3) * square, (cols + 3) * square), 255, np.uint8)
    for r in range(rows + 1):
        for c in range(cols + 1):
            if (r + c) % 2 == 0:
                texture[(r + 1) * square : (r + 2) * square, (c + 1) * square : (c + 2) * square] = 0
    width, height = truth["width"], truth["height"]
    matrix = np.array([[truth["fx"], 0, truth["cx"]], [0, truth["fy"], truth["cy"]], [0, 0, 1]], np.float64)
    distortion = np.array([truth["k1"], truth["k2"], truth["p1"], truth["p2"]], np.float64)
    pixels = np.stack(np.meshgrid(np.arange(width), np.arange(height)), -1).reshape(-1, 1, 2).astype(np.float64)
    rays = cv2.undistortPoints(pixels, matrix, distortion).reshape(-1, 2)
    rays = np.hstack([rays, np.ones((len(rays), 1))])
    for _ in range(count):
        rvec = rng.normal(0, 0.28, 3)
        rotation, _ = cv2.Rodrigues(rvec)
        centre = np.array([(cols - 1) / 2, (rows - 1) / 2, 0.0])
        distance = rng.uniform(9, 17)
        offset = np.array([rng.uniform(-3.5, 3.5), rng.uniform(-2.5, 2.5), distance])
        translation = offset - rotation @ centre
        # Ray from the camera through each pixel, intersected with the board plane z = 0.
        direction = rays @ rotation  # rows are R^T d
        origin = -rotation.T @ translation
        scale = -origin[2] / direction[:, 2]
        points = origin + direction * scale[:, None]
        map_x = ((points[:, 0] + 1.5) * square).reshape(height, width).astype(np.float32)
        map_y = ((points[:, 1] + 1.5) * square).reshape(height, width).astype(np.float32)
        view = cv2.remap(texture, map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=140)
        view = cv2.GaussianBlur(view, (3, 3), 0.8).astype(np.int16) + rng.normal(0, 3, view.shape).astype(np.int16)
        ok, encoded = cv2.imencode(".jpg", np.clip(view, 0, 255).astype(np.uint8), [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        yield cv2.imdecode(encoded, cv2.IMREAD_GRAYSCALE) if ok else None


def selftest(cols: int, rows: int) -> int:
    truth = {"width": 320, "height": 240, "fx": 251.0, "fy": 249.0, "cx": 163.0, "cy": 117.0, "k1": -0.21, "k2": 0.06, "p1": 0.001, "p2": -0.0015}
    views, size, examined, found = detect(synthetic_views(cols, rows, 45, truth), cols, rows)
    print(f"Self-test with drawn boards and a known lens: {examined} pictures, board found in {found}, {len(views)} views kept.")
    if len(views) < MIN_VIEWS:
        print("SELF-TEST FAILED: too few boards were found.")
        return 1
    result = calibrate(views, size, cols, rows, 40)
    print(describe(result))
    failures = problems(result)
    for key, tolerance in (("fx", 0.02 * truth["fx"]), ("fy", 0.02 * truth["fy"]), ("cx", 3.0), ("cy", 3.0), ("k1", 0.03), ("k2", 0.08)):
        error = abs(result[key] - truth[key])
        verdict = "ok  " if error <= tolerance else "FAIL"
        print(f"  {verdict}  {key}: true {truth[key]:.4f}, measured {result[key]:.4f}, off by {error:.4f} (allowed {tolerance:.4f})")
        if error > tolerance:
            failures.append(f"{key} off by {error:.4f}")
    if failures:
        print("SELF-TEST FAILED: " + "; ".join(failures))
        return 1
    print("Self-test passed. This checks the method on drawn pictures. It is not a calibration of the ring.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate the ring camera from a chessboard recording. Reads files only.")
    parser.add_argument("recording", nargs="?", default="", help="recording folder. Default: the newest one.")
    parser.add_argument("--cols", type=int, default=9, help="inner corners across (default 9)")
    parser.add_argument("--rows", type=int, default=6, help="inner corners down (default 6)")
    parser.add_argument("--every", type=int, default=2, help="examine every Nth frame (default 2)")
    parser.add_argument("--max-views", type=int, default=40)
    parser.add_argument("--selftest", action="store_true", help="check the method on drawn boards; no camera, writes nothing")
    args = parser.parse_args()
    if args.selftest:
        return selftest(args.cols, args.rows)

    folder = Path(args.recording) if args.recording else newest_recording()
    if not (folder / "frames.csv").is_file():
        raise SystemExit(f"{folder} is not a ring recording (no frames.csv).")
    print(f"Looking for a {args.cols}x{args.rows} inner-corner chessboard in {folder}")
    views, size, examined, found = detect(recording_images(folder, max(1, args.every)), args.cols, args.rows)
    print(f"Pictures examined {examined}. Board found in {found}. Different views kept {len(views)}.")
    if len(views) < 4:
        print(
            "NOT CALIBRATED. The board was found in too few pictures to fit anything. "
            "Check the corner counts (--cols, --rows), the lighting, and that the whole board is in view."
        )
        return 2
    result = calibrate(views, size, args.cols, args.rows, args.max_views)
    print(describe(result))
    failures = problems(result)
    if failures:
        print("NOT CALIBRATED. Nothing was written, because:")
        for failure in failures:
            print(f"  - {failure}")
        return 2
    meta = {}
    if (folder / "meta.json").is_file():
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    record = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "source_recording": str(folder),
        "sensor_line": meta.get("sensor_line", ""),
        "pattern": f"chessboard {args.cols}x{args.rows} inner corners",
        "model": "pinhole with radial k1, k2 and tangential p1, p2 (OpenCV order); k3 fixed at 0",
        "valid_for": "this lens and focus at this picture size only",
        **{key: value for key, value in result.items() if key != "coverage_map"},
    }
    CALIBRATION_FILE.parent.mkdir(exist_ok=True)
    if CALIBRATION_FILE.exists():
        kept = CALIBRATION_FILE.with_name(f"ring_camera-replaced-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        shutil.move(str(CALIBRATION_FILE), str(kept))
        print(f"The previous calibration was kept as {kept.name}.")
    CALIBRATION_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"CALIBRATED. Written to {CALIBRATION_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
