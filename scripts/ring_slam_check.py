"""Check one ring recording against what ORB-SLAM3 needs. Reads files only.

    .\\.venv\\Scripts\\python.exe scripts\\ring_slam_check.py                 (newest recording)
    .\\.venv\\Scripts\\python.exe scripts\\ring_slam_check.py <recording folder>

It measures the recording made by ring_record.py and prints, with the numbers:
  1. timing: camera rate, IMU rate, IMU samples per frame, timestamp quality
  2. picture content: features per frame, blur, brightness
  3. frame-to-frame tracking: how many feature matches agree on one camera motion
  4. which prerequisites for monocular and monocular-inertial ORB-SLAM3 exist

The feature and tracking numbers come from OpenCV's ORB and a fundamental-matrix
check between neighbouring frames. That is a screening test, not ORB-SLAM3
itself. The two thresholds it uses are ORB-SLAM3's own (src/Tracking.cc):
more than 100 keypoints to start a monocular map, and at least 30 matched
map points to keep tracking. Nothing here needs or invents a calibration.

The report is also written to slam_check.txt in the recording folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FOOTAGE_DIR = ROOT / "recordings" / "ring_footage"
CALIBRATION_FILE = ROOT / "calibration" / "ring_camera.json"

ORB_FEATURES = 1000  # ORBextractor.nFeatures in ORB-SLAM3's example settings
ORB_SCALE = 1.2
ORB_LEVELS = 8
INIT_KEYPOINTS = 100  # Tracking.cc: mCurrentFrame.mvKeys.size()>100
TRACK_INLIERS = 30  # Tracking.cc: mnMatchesInliers<30 fails local-map tracking (monocular)
LOWE_RATIO = 0.75
STILL_PX = 1.0  # median match movement below this: the camera did not move between the two frames
ATTACHED_PX = 1.0  # a match that stays put while the rest move is on something attached to the camera
MOVING_PX = 3.0
GRID_COLS, GRID_ROWS = 8, 6
EXAMPLE_IMU_HZ = 200.0  # IMU.Frequency in ORB-SLAM3's Examples/Monocular-Inertial/EuRoC.yaml
SATURATED = 32760


def newest_recording() -> Path:
    folders = sorted(path for path in FOOTAGE_DIR.glob("*") if (path / "frames.csv").is_file())
    if not folders:
        raise SystemExit(f"No recording found under {FOOTAGE_DIR}. Run scripts\\ring_record.py first.")
    return folders[-1]


def pct(values, q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q)) if len(values) else float("nan")


def load(folder: Path) -> tuple[list[dict], list[dict], dict]:
    with (folder / "frames.csv").open(encoding="utf-8", newline="") as handle:
        frames = list(csv.DictReader(handle))
    with (folder / "imu.csv").open(encoding="utf-8", newline="") as handle:
        imu = list(csv.DictReader(handle))
    meta = {}
    if (folder / "meta.json").is_file():
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    return frames, imu, meta


def timing(frames: list[dict], imu: list[dict]) -> tuple[list[str], dict]:
    lines: list[str] = []
    facts: dict = {}
    host = np.array([int(row["host_mono_ns"]) for row in frames], dtype=np.float64) / 1e6
    seconds = (host[-1] - host[0]) / 1000.0
    gaps = np.diff(host)
    facts["camera_fps"] = (len(frames) - 1) / seconds
    lines.append(f"  Camera frames {len(frames)} over {seconds:.1f} s: {facts['camera_fps']:.1f} per second.")
    lines.append(
        f"  Gap between frames as read by the laptop: median {np.median(gaps):.0f} ms, "
        f"95% under {pct(gaps, 95):.0f} ms, longest {gaps.max():.0f} ms."
    )
    stamped = [(index, int(row["board_ms_before_capture"])) for index, row in enumerate(frames) if row["board_ms_before_capture"]]
    facts["frames_without_board_stamp"] = len(frames) - len(stamped)
    lines.append("  Capture time of each picture on the board's clock: NOT SENT by the firmware.")
    if len(stamped) > 2:
        board = np.array([value for _index, value in stamped], dtype=np.float64)
        board_gaps = np.diff(board)
        offset = host[[index for index, _value in stamped]] - board
        offset -= np.median(offset)
        facts["host_vs_board_spread_ms"] = pct(offset, 95) - pct(offset, 5)
        lines.append(
            f"  Closest substitute, the IMU reading sent just before each frame: {len(stamped)} of {len(frames)} frames have one. "
            f"Step median {np.median(board_gaps):.0f} ms, longest {board_gaps.max():.0f} ms."
        )
        lines.append(
            f"  Laptop read time against that board clock wanders by {facts['host_vs_board_spread_ms']:.0f} ms "
            f"(5th to 95th percentile), worst {offset.min():.0f} to {offset.max():+.0f} ms."
        )
    if len(imu) > 2:
        board_ms = np.array([int(row["board_ms"]) for row in imu], dtype=np.float64)
        steps = np.diff(board_ms)
        facts["imu_hz"] = (len(imu) - 1) / ((board_ms[-1] - board_ms[0]) / 1000.0)
        lines.append(
            f"  IMU samples {len(imu)}: {facts['imu_hz']:.1f} per second on the board clock. "
            f"Step median {np.median(steps):.0f} ms, longest {steps.max():.0f} ms."
        )
        if len(stamped) > 2:
            per_frame = Counter()
            edges = [value for _index, value in stamped]
            counts = np.searchsorted(board_ms, edges[1:], side="right") - np.searchsorted(board_ms, edges[:-1], side="right")
            per_frame.update(int(value) for value in counts)
            facts["imu_per_frame"] = float(np.mean(counts))
            spread = ", ".join(f"{count} sample(s): {number} intervals" for count, number in sorted(per_frame.items()))
            lines.append(f"  IMU samples between one frame and the next: {spread}.")
        raw = np.array([[int(row[key]) for key in ("ax_raw", "ay_raw", "az_raw", "gx_raw", "gy_raw", "gz_raw")] for row in imu])
        gyro = np.array([[abs(float(row[key])) for key in ("gx_dps", "gy_dps", "gz_dps")] for row in imu])
        accel = np.array([[float(row[key]) for key in ("ax_g", "ay_g", "az_g")] for row in imu])
        magnitude = np.linalg.norm(accel, axis=1)
        facts["imu_saturated"] = int((np.abs(raw) >= SATURATED).any(axis=1).sum())
        lines.append(
            f"  Gyro: median peak {np.median(gyro.max(axis=1)):.0f} deg/s, 95% under {pct(gyro.max(axis=1), 95):.0f}, "
            f"highest {gyro.max():.0f} (range limit 250). Accel magnitude mean {magnitude.mean():.2f} g, "
            f"{magnitude.min():.2f} to {magnitude.max():.2f}. Samples at the range limit: {facts['imu_saturated']}."
        )
    else:
        facts["imu_hz"] = 0.0
        lines.append("  No IMU samples in this recording.")
    return lines, facts


def content(folder: Path, frames: list[dict], every: int) -> tuple[list[str], dict]:
    orb = cv2.ORB_create(nfeatures=ORB_FEATURES, scaleFactor=ORB_SCALE, nlevels=ORB_LEVELS)
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    chosen = frames[::every]
    times = np.array([int(row["host_mono_ns"]) for row in chosen], dtype=np.float64) / 1e9
    features, blur, level = [], [], []
    inliers, movement, attached_share, still = [], [], [], []
    grid = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.float64)
    attached_grid = np.zeros((GRID_ROWS, GRID_COLS), dtype=np.float64)
    previous = None
    width = height = 0
    for row in chosen:
        image = cv2.imread(str(folder / row["file"]), cv2.IMREAD_GRAYSCALE)
        if image is None:
            features.append(0)
            blur.append(0.0)
            level.append(0.0)
            inliers.append(0)
            previous = None
            continue
        height, width = image.shape[:2]
        keypoints, descriptors = orb.detectAndCompute(image, None)
        keypoints = list(keypoints or [])
        features.append(len(keypoints))
        blur.append(float(cv2.Laplacian(image, cv2.CV_64F).var()))
        level.append(float(image.mean()))
        current = (keypoints, descriptors)
        if previous is not None:
            count = 0
            prev_kp, prev_desc = previous
            if descriptors is not None and prev_desc is not None and len(keypoints) >= 2 and len(prev_kp) >= 2:
                good = [
                    pair[0]
                    for pair in matcher.knnMatch(descriptors, prev_desc, k=2)
                    if len(pair) == 2 and pair[0].distance < LOWE_RATIO * pair[1].distance
                ]
                if len(good) >= 8:
                    now_pts = np.float32([keypoints[match.queryIdx].pt for match in good])
                    old_pts = np.float32([prev_kp[match.trainIdx].pt for match in good])
                    moved = np.linalg.norm(now_pts - old_pts, axis=1)
                    typical = float(np.median(moved))
                    movement.append(typical)
                    _matrix, mask = cv2.findFundamentalMat(old_pts, now_pts, cv2.FM_RANSAC, 1.0, 0.99)
                    keep = np.zeros(len(good), dtype=bool) if mask is None else mask.ravel().astype(bool)
                    if typical < STILL_PX:
                        still.append(True)
                        count = int(keep.sum())
                    else:
                        still.append(False)
                        stuck = moved < ATTACHED_PX
                        if typical >= MOVING_PX:
                            attached_share.append(float(stuck.mean()))
                            for x, y in now_pts[stuck]:
                                attached_grid[min(GRID_ROWS - 1, int(y * GRID_ROWS / height)), min(GRID_COLS - 1, int(x * GRID_COLS / width))] += 1
                        scene = keep & ~stuck
                        count = int(scene.sum())
                        for x, y in now_pts[scene]:
                            grid[min(GRID_ROWS - 1, int(y * GRID_ROWS / height)), min(GRID_COLS - 1, int(x * GRID_COLS / width))] += 1
            inliers.append(count)
        previous = current

    features_arr = np.array(features)
    inliers_arr = np.array(inliers)
    lines: list[str] = []
    facts: dict = {}
    facts["frames_enough_keypoints"] = float((features_arr > INIT_KEYPOINTS).mean())
    lines.append(f"  Frames examined: {len(chosen)}" + (f" (every {every}th)" if every > 1 else "") + f", {width}x{height} pixels.")
    lines.append(
        f"  ORB keypoints per frame (asked for {ORB_FEATURES}): median {np.median(features_arr):.0f}, "
        f"lowest tenth under {pct(features_arr, 10):.0f}, minimum {features_arr.min()}."
    )
    lines.append(
        f"  Frames with more than {INIT_KEYPOINTS} keypoints, which ORB-SLAM3 needs to start a monocular map: "
        f"{100 * facts['frames_enough_keypoints']:.0f}%."
    )
    lines.append(
        f"  Sharpness (Laplacian variance, higher is sharper): median {np.median(blur):.0f}, lowest tenth under {pct(blur, 10):.0f}. "
        f"Brightness (0 to 255): median {np.median(level):.0f}, {min(level):.0f} to {max(level):.0f}."
    )
    track: list[str] = []
    weak = inliers_arr < TRACK_INLIERS
    facts["weak_pair_share"] = float(weak.mean()) if len(weak) else 1.0
    longest = run = 0
    longest_good = good_run = 0
    for flag in weak:
        run = run + 1 if flag else 0
        good_run = 0 if flag else good_run + 1
        longest = max(longest, run)
        longest_good = max(longest_good, good_run)
    period = float(np.median(np.diff(times))) if len(times) > 1 else 0.0
    facts["longest_weak_s"] = longest * period
    facts["longest_good_s"] = longest_good * period
    track.append(
        f"  Matches between neighbouring frames that agree on one camera motion: median {np.median(inliers_arr):.0f}, "
        f"lowest tenth under {pct(inliers_arr, 10):.0f}."
    )
    track.append(
        f"  Frame pairs under {TRACK_INLIERS}, the level where ORB-SLAM3 monocular tracking fails: {100 * facts['weak_pair_share']:.0f}%. "
        f"Longest stretch under it: {facts['longest_weak_s']:.1f} s. Longest stretch above it: {facts['longest_good_s']:.1f} s."
    )
    if movement:
        facts["still_share"] = float(np.mean(still))
        track.append(
            f"  Picture movement between frames: median {np.median(movement):.1f} pixels, 95% under {pct(movement, 95):.1f}. "
            f"Camera effectively still in {100 * facts['still_share']:.0f}% of pairs."
        )
    else:
        track.append("  Too few matches in every frame pair to measure picture movement.")
    if attached_share:
        facts["attached_share"] = float(np.median(attached_share))
        track.append(
            f"  While the camera moved, {100 * facts['attached_share']:.0f}% of matches (median) stayed fixed in the picture. "
            "Those are on something that moves with the camera, such as a finger, not on the room."
        )
    total = grid.sum()
    if total > 0:
        share = grid / total
        facts["grid_coverage"] = float((share >= 0.01).mean())
        track.append(
            f"  Where the room matches fall, as tenths of the busiest cell ({GRID_COLS}x{GRID_ROWS} grid, '.' is none). "
            f"{100 * facts['grid_coverage']:.0f}% of the picture area contributes."
        )
        peak = grid.max()
        for cells in grid:
            track.append("    " + " ".join("." if value == 0 else str(min(9, int(9 * value / peak))) for value in cells))
    else:
        facts["grid_coverage"] = 0.0
        track.append("  No matches on the room while the camera moved, so there is no coverage map.")
    return lines + ["", "FRAME-TO-FRAME TRACKING (screening, not ORB-SLAM3 itself)"] + track, facts


def prerequisites(facts: dict, meta: dict) -> list[str]:
    lines: list[str] = []
    calibrated = CALIBRATION_FILE.is_file()
    footage_ok = (
        facts.get("frames_enough_keypoints", 0) >= 0.9
        and facts.get("weak_pair_share", 1) <= 0.1
        and facts.get("longest_weak_s", 99) < 1.0
        and facts.get("still_share", 1) < 0.8
    )
    lines.append("  Monocular (camera only)")
    lines.append(
        "    [" + ("ok     " if calibrated else "MISSING") + "] Camera calibration (focal length, centre, lens distortion) at "
        f"{meta.get('width', '?')}x{meta.get('height', '?')}: "
        + (f"found {CALIBRATION_FILE}" if calibrated else "none on file. Make one with scripts\\ring_calibrate_camera.py.")
    )
    reasons = []
    if facts.get("frames_enough_keypoints", 0) < 0.9:
        reasons.append(f"only {100 * facts.get('frames_enough_keypoints', 0):.0f}% of frames have more than {INIT_KEYPOINTS} keypoints")
    if facts.get("weak_pair_share", 1) > 0.1:
        reasons.append(f"{100 * facts.get('weak_pair_share', 1):.0f}% of frame pairs are under {TRACK_INLIERS} agreeing matches")
    if facts.get("longest_weak_s", 99) >= 1.0:
        reasons.append(f"tracking would be starved for {facts.get('longest_weak_s', 0):.1f} s in a row")
    if facts.get("still_share", 1) >= 0.8:
        reasons.append("the camera barely moved, and a monocular map needs the camera to change position")
    lines.append(
        "    [" + ("ok     " if footage_ok else "FAILS  ") + "] This recording's pictures: "
        + ("pass the screening." if footage_ok else "; ".join(reasons) + ".")
    )
    lines.append("    [limit  ] One camera cannot measure real size. A monocular map has no metres; distances are relative only.")
    lines.append("    [limit  ] The OV3660 is a rolling-shutter sensor and ORB-SLAM3 assumes all rows are exposed at once. Fast hand motion will bend the picture.")
    lines.append("")
    lines.append("  Monocular-inertial (camera + MPU6050)")
    imu_hz = facts.get("imu_hz", 0.0)
    lines.append(
        f"    [FAILS  ] IMU rate: {imu_hz:.0f} samples per second, {facts.get('imu_per_frame', 0):.1f} per frame. "
        f"ORB-SLAM3's example settings use {EXAMPLE_IMU_HZ:.0f} per second, about {EXAMPLE_IMU_HZ / max(facts.get('camera_fps', 1), 1):.0f} per frame at this frame rate. "
        "With one reading per frame it holds that single reading for the whole gap between frames."
    )
    lines.append("    [MISSING] Picture capture time on the board clock. The firmware sends none, so pictures and IMU readings cannot be lined up to the few milliseconds this mode needs.")
    lines.append("    [MISSING] Camera-to-IMU alignment (rotation and offset between the camera and the GY-521 board). Never measured.")
    lines.append("    [MISSING] IMU noise and bias-drift figures for this MPU6050. Never measured.")
    lines.append("    [MISSING] Camera calibration, as above." if not calibrated else "    [ok     ] Camera calibration.")
    lines.append("    The first two need a firmware change. Nothing here changes or flashes the firmware.")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen a ring recording for ORB-SLAM3. Reads files only.")
    parser.add_argument("recording", nargs="?", default="", help="recording folder. Default: the newest one.")
    parser.add_argument("--every", type=int, default=1, help="examine every Nth frame (default 1, every frame)")
    args = parser.parse_args()
    folder = Path(args.recording) if args.recording else newest_recording()
    if not (folder / "frames.csv").is_file():
        raise SystemExit(f"{folder} is not a ring recording (no frames.csv).")
    frames, imu, meta = load(folder)
    if len(frames) < 10:
        raise SystemExit(f"{folder} has only {len(frames)} frames. Record at least a few seconds.")

    out = [f"SLAM screening of {folder}", f"Label: {meta.get('label') or 'none'}. Recorded {meta.get('started', 'unknown')}.", ""]
    out.append("TIMING")
    timing_lines, facts = timing(frames, imu)
    out += timing_lines
    out += ["", "PICTURE CONTENT"]
    content_lines, content_facts = content(folder, frames, max(1, args.every))
    facts.update(content_facts)
    out += content_lines
    out += ["", "WHAT ORB-SLAM3 NEEDS, AND WHAT EXISTS"]
    out += prerequisites(facts, meta)
    out += [
        "",
        "This is a screening of recorded files. ORB-SLAM3 itself was not run.",
    ]
    text = "\n".join(out) + "\n"
    (folder / "slam_check.txt").write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
