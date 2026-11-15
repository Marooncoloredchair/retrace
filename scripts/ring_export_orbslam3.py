"""Turn one ring recording into the folder layout ORB-SLAM3's mono_euroc example reads.

    .\\.venv\\Scripts\\python.exe scripts\\ring_export_orbslam3.py                     (newest recording)
    .\\.venv\\Scripts\\python.exe scripts\\ring_export_orbslam3.py <recording folder>

Monocular only. It writes, inside the recording folder:

  orbslam3/mav0/cam0/data/<time in ns>.png   grayscale copies of the frames
  orbslam3/timestamps.txt                    one time per line, the list mono_euroc reads
  orbslam3/ring_mono.yaml                    settings built from calibration/ring_camera.json
  orbslam3/export_info.json                  where the numbers came from

It refuses to run without a measured camera calibration, because the settings
file would otherwise contain invented lens numbers. It never writes a
monocular-inertial settings file: that mode needs a camera-to-IMU alignment,
IMU noise figures and a faster IMU stream, none of which exist yet.

Frame times are the laptop's read times, not capture times (see ring_record.py).
For monocular mapping they only order the frames.

This export has been checked against the file layout mono_euroc.cc expects.
It has not yet been run through ORB-SLAM3, which is not installed on this laptop.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
FOOTAGE_DIR = ROOT / "recordings" / "ring_footage"
CALIBRATION_FILE = ROOT / "calibration" / "ring_camera.json"
CALIBRATION_KEYS = ("width", "height", "fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2", "rms_px", "views")

SETTINGS = """%YAML:1.0

# ReTrace ring camera, monocular. Written by scripts/ring_export_orbslam3.py.
# Camera numbers come from {calibration} (measured {created}, fit error {rms_px:.3f} px, {views} views).
File.version: "1.0"

Camera.type: "PinHole"

Camera1.fx: {fx:.6f}
Camera1.fy: {fy:.6f}
Camera1.cx: {cx:.6f}
Camera1.cy: {cy:.6f}

Camera1.k1: {k1:.8f}
Camera1.k2: {k2:.8f}
Camera1.p1: {p1:.8f}
Camera1.p2: {p2:.8f}

Camera.width: {width}
Camera.height: {height}

# Measured from this recording
Camera.fps: {fps}

# Images are exported as grayscale, so this is ignored
Camera.RGB: 1

# ORB extractor: the values from ORB-SLAM3's Examples/Monocular/EuRoC.yaml
ORBextractor.nFeatures: 1000
ORBextractor.scaleFactor: 1.2
ORBextractor.nLevels: 8
ORBextractor.iniThFAST: 20
ORBextractor.minThFAST: 7

Viewer.KeyFrameSize: 0.05
Viewer.KeyFrameLineWidth: 1.0
Viewer.GraphLineWidth: 0.9
Viewer.PointSize: 2.0
Viewer.CameraSize: 0.08
Viewer.CameraLineWidth: 3.0
Viewer.ViewpointX: 0.0
Viewer.ViewpointY: -0.7
Viewer.ViewpointZ: -1.8
Viewer.ViewpointF: 500.0
"""


def newest_recording() -> Path:
    folders = sorted(path for path in FOOTAGE_DIR.glob("*") if (path / "frames.csv").is_file())
    if not folders:
        raise SystemExit(f"No recording found under {FOOTAGE_DIR}. Run scripts\\ring_record.py first.")
    return folders[-1]


def wsl_path(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    return f"/mnt/{drive}" + resolved.as_posix()[len(resolved.drive):]


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a ring recording for ORB-SLAM3 mono_euroc. Monocular only.")
    parser.add_argument("recording", nargs="?", default="", help="recording folder. Default: the newest one.")
    parser.add_argument("--calibration", default=str(CALIBRATION_FILE), help="camera calibration file")
    parser.add_argument("--out", default="", help="output folder. Default: <recording>/orbslam3")
    args = parser.parse_args()

    calibration_path = Path(args.calibration)
    if not calibration_path.is_file():
        print(
            f"Not exported. There is no camera calibration at {calibration_path}.\n"
            "ORB-SLAM3 needs the measured focal length, optical centre and lens distortion, and this tool will not invent them.\n"
            "Record a chessboard with scripts\\ring_record.py, then run scripts\\ring_calibrate_camera.py.",
            file=sys.stderr,
        )
        return 2
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    missing = [key for key in CALIBRATION_KEYS if key not in calibration]
    if missing:
        print(f"Not exported. {calibration_path} is missing: {', '.join(missing)}.", file=sys.stderr)
        return 2

    folder = Path(args.recording) if args.recording else newest_recording()
    if not (folder / "frames.csv").is_file():
        raise SystemExit(f"{folder} is not a ring recording (no frames.csv).")
    with (folder / "frames.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) < 30:
        raise SystemExit(f"{folder} has only {len(rows)} frames.")
    first = cv2.imread(str(folder / rows[0]["file"]), cv2.IMREAD_GRAYSCALE)
    if first is None:
        raise SystemExit(f"Could not read {rows[0]['file']}.")
    height, width = first.shape[:2]
    if (width, height) != (int(calibration["width"]), int(calibration["height"])):
        print(
            f"Not exported. The recording is {width}x{height} and the calibration is for "
            f"{calibration['width']}x{calibration['height']}. A calibration only holds at the picture size it was measured at.",
            file=sys.stderr,
        )
        return 2

    out = Path(args.out) if args.out else folder / "orbslam3"
    data = out / "mav0" / "cam0" / "data"
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} already has files in it. Choose another --out or remove it yourself.")
    data.mkdir(parents=True)
    stamps: list[int] = []
    skipped = 0
    for row in rows:
        image = cv2.imread(str(folder / row["file"]), cv2.IMREAD_GRAYSCALE)
        if image is None:
            skipped += 1
            continue
        stamp = int(row["host_mono_ns"])
        if stamps and stamp <= stamps[-1]:
            stamp = stamps[-1] + 1
        cv2.imwrite(str(data / f"{stamp}.png"), image)
        stamps.append(stamp)
    (out / "timestamps.txt").write_text("".join(f"{stamp}\n" for stamp in stamps), encoding="ascii", newline="\n")
    seconds = (stamps[-1] - stamps[0]) / 1e9
    fps = round((len(stamps) - 1) / seconds) if seconds > 0 else 0
    settings = SETTINGS.format(
        calibration=calibration_path.name,
        created=calibration.get("created", "unknown date"),
        fps=fps,
        **{key: calibration[key] for key in CALIBRATION_KEYS},
    )
    (out / "ring_mono.yaml").write_text(settings, encoding="ascii", newline="\n")
    info = {
        "source_recording": str(folder),
        "frames_exported": len(stamps),
        "frames_skipped": skipped,
        "seconds": round(seconds, 3),
        "fps_written_to_settings": fps,
        "timestamps": "laptop monotonic read time in nanoseconds; not the capture time",
        "calibration_file": str(calibration_path),
        "calibration": {key: calibration[key] for key in CALIBRATION_KEYS},
        "mode": "monocular only; no IMU data and no inertial settings are exported",
        "run_through_orbslam3": False,
    }
    (out / "export_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"Exported {len(stamps)} frames ({seconds:.1f} s, {fps} per second) to {out}")
    print("To run it, in a Linux shell where ORB-SLAM3 is built (for example WSL), from the ORB_SLAM3 folder:")
    print(
        "  ./Examples/Monocular/mono_euroc Vocabulary/ORBvoc.txt "
        f"{wsl_path(out)}/ring_mono.yaml {wsl_path(out)} {wsl_path(out)}/timestamps.txt ring"
    )
    print("It writes f_ring.txt (camera path) and kf_ring.txt (keyframe path). A monocular path has no real-world scale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
