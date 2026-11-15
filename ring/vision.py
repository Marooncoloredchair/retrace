"""Frame measurements, observation vs landmark decisions, occlusion, and verified comparison.

Features come from app.analysis.analyze_packet (ORB, 500 features) and the
comparison from app.places.match_descriptors (ratio test, then a RANSAC
homography). Both are used unchanged for the core match.

This file adds:
  * a soft occlusion mask (finger / flat cover over part of the lens)
  * matching that prefers keypoints in the visible part of each picture
  * separate rules for "remember this observation" and "use this as a landmark"
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from app.analysis import analyze_packet
from app.places import match_descriptors

GRID_COLS = 4
GRID_ROWS = 3

REASON_TEXT = {
    "dark": "too dark",
    "bright": "too bright",
    "obstructed": "camera obstructed (lens covered, or facing a plain surface)",
    "few_features": "too little detail",
    "blurry": "blurry",
    "covered": "partly covered",
    "fast_motion": "moving too fast",
    "duplicate": "same view as a frame already kept",
    "too_soon": "too soon after the last kept frame",
    "undecodable": "not a readable picture",
    "storage_error": "could not be written to disk",
    "observed": "recorded as an observation",
    "landmark": "recorded as a landmark",
}


def lift_brightness_bgr(
    image: np.ndarray,
    target_mean: float = 110.0,
    max_gain: float = 5.0,
) -> np.ndarray:
    """Raise dim indoor frames toward target_mean gray level (gain + mild gamma)."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    if mean < 1.0:
        return image
    gain = min(float(max_gain), float(target_mean) / mean)
    if abs(gain - 1.0) < 0.05:
        return image
    lifted = np.clip(image.astype(np.float32) * gain, 0.0, 255.0)
    mid = float(cv2.cvtColor(lifted.astype(np.uint8), cv2.COLOR_BGR2GRAY).mean())
    if mid < float(target_mean) * 0.85 and mid > 1.0:
        # gamma < 1 brightens midtones after gain has started clipping highlights
        ratio = max(float(target_mean) / 255.0, 1e-6)
        base = max(mid / 255.0, 1e-6)
        gamma = max(0.35, min(0.95, math.log(ratio) / math.log(base)))
        lifted = np.clip(255.0 * np.power(lifted / 255.0, gamma), 0.0, 255.0)
    return lifted.astype(np.uint8)


def lift_brightness_jpeg(
    jpeg: bytes,
    target_mean: float = 110.0,
    max_gain: float = 5.0,
    quality: int = 92,
) -> bytes:
    """Decode → lift → re-encode. Returns original bytes if decode fails."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg
    lifted = lift_brightness_bgr(image, target_mean=target_mean, max_gain=max_gain)
    ok, encoded = cv2.imencode(".jpg", lifted, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    return encoded.tobytes() if ok else jpeg


def analyse(
    jpeg: bytes,
    seq: int,
    unix_ms: int,
    *,
    brightness_lift: bool = False,
    brightness_target_mean: float = 110.0,
    brightness_max_gain: float = 5.0,
) -> dict | None:
    """Decode one camera JPEG and measure it. None when it cannot be decoded.

    When brightness_lift is on, metrics run on a lifted copy; result['jpeg'] stays
    the original camera bytes for storage.
    """
    analysis_jpeg = jpeg
    if brightness_lift:
        analysis_jpeg = lift_brightness_jpeg(
            jpeg,
            target_mean=brightness_target_mean,
            max_gain=brightness_max_gain,
            quality=95,
        )
    packet = (
        (seq & 0xFFFFFFFF).to_bytes(4, "big")
        + (unix_ms & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
        + analysis_jpeg
    )
    result = analyze_packet(packet)
    if result is None:
        return None
    width, height = result["width"], result["height"]
    gray = cv2.cvtColor(result["image"], cv2.COLOR_BGR2GRAY)
    mask = occlusion_mask(result["image"])
    skin = skin_mask(result["image"])
    finger = cv2.bitwise_or(mask, skin)
    occlusion = float(mask.mean()) / 255.0
    finger_fraction = float(finger.mean()) / 255.0
    dark_fraction = float((gray < 28).mean())
    visible_kp = []
    visible_desc_rows = []
    descriptors = result["descriptors"]
    cells = set()
    for index, point in enumerate(result["keypoints"]):
        x, y = int(point.pt[0]), int(point.pt[1])
        if 0 <= x < width and 0 <= y < height and finger[y, x] == 0:
            visible_kp.append(point)
            if descriptors is not None:
                visible_desc_rows.append(descriptors[index])
            column = min(GRID_COLS - 1, int(point.pt[0] * GRID_COLS / width))
            row = min(GRID_ROWS - 1, int(point.pt[1] * GRID_ROWS / height))
            cells.add((column, row))
    # Coverage and feature counts for landmark decisions use the visible region only.
    result["occlusion"] = round(occlusion, 3)
    result["occlusion_mask"] = mask
    result["finger_mask"] = finger
    result["finger_fraction"] = round(finger_fraction, 3)
    result["dark_fraction"] = round(dark_fraction, 3)
    grid = grid_cover_measures(result["image"])
    result["defocus_fraction"] = grid["defocus"]
    result["skin_fraction"] = grid["skin"]
    result["grid_dark_fraction"] = grid["dark"]
    result["cover_fraction"] = grid["cover"]
    result["visible_keypoints"] = visible_kp
    result["visible_descriptors"] = (
        None if not visible_desc_rows else np.ascontiguousarray(visible_desc_rows, dtype=np.uint8)
    )
    result["visible_feature_count"] = len(visible_kp)
    result["coverage"] = round(len(cells) / (GRID_COLS * GRID_ROWS), 3)
    result["brightness"] = round(float(result["image"].mean()), 1)
    result["contrast"] = round(float(gray.std()), 1)
    result["jpeg"] = jpeg  # always original camera bytes
    result["brightness_lift"] = bool(brightness_lift)
    return result


def grid_cover_measures(image: np.ndarray, cols: int = 8, rows: int = 6) -> dict:
    """Cover on an 8x6 grid. Defocus, dark, and skin are fractions of cells, logged apart.

    A cell is defocused when it is much blurrier than the sharper cells in the same
    frame, so a uniformly sharp view is not marked covered.
    """
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    skin = skin_mask(image)
    lap = cv2.Laplacian(gray, cv2.CV_32F)
    cells = []
    for row in range(rows):
        y0, y1 = row * height // rows, (row + 1) * height // rows
        for col in range(cols):
            x0, x1 = col * width // cols, (col + 1) * width // cols
            cell_gray = gray[y0:y1, x0:x1]
            cell_skin = skin[y0:y1, x0:x1]
            cell_lap = lap[y0:y1, x0:x1]
            if cell_gray.size == 0:
                continue
            cells.append(
                {
                    "var": float(cell_lap.var()),
                    "dark": float((cell_gray < 28).mean()) >= 0.70,
                    "skin": float((cell_skin > 0).mean()) >= 0.50,
                }
            )
    total = max(1, len(cells))
    sharp = sorted(cell["var"] for cell in cells)
    mid = sharp[len(sharp) // 2] if sharp else 0.0
    defocus_n = dark_n = skin_n = covered = 0
    for cell in cells:
        defocus = cell["var"] < 40.0 and cell["var"] < 0.35 * mid
        if defocus:
            defocus_n += 1
        if cell["dark"]:
            dark_n += 1
        if cell["skin"]:
            skin_n += 1
        if defocus or cell["dark"] or cell["skin"]:
            covered += 1
    return {
        "defocus": round(defocus_n / total, 3),
        "dark": round(dark_n / total, 3),
        "skin": round(skin_n / total, 3),
        "cover": round(covered / total, 3),
    }


def crop_finger_free_jpeg(jpeg: bytes, finger: np.ndarray | None = None) -> bytes:
    """Crop to the largest finger-free box. The original bytes are kept if the crop is tiny."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg
    if finger is None:
        finger = cv2.bitwise_or(occlusion_mask(image), skin_mask(image))
    if finger.shape[:2] != image.shape[:2]:
        finger = cv2.resize(finger, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
    clear = finger == 0
    if int(clear.sum()) < 800:
        return jpeg
    ys, xs = np.where(clear)
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    if (x1 - x0) < 32 or (y1 - y0) < 32:
        return jpeg
    ok, buf = cv2.imencode(".jpg", image[y0:y1, x0:x1], [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        return jpeg
    return buf.tobytes()


def find_frame_ok(result: dict, imu: dict | None, config) -> bool:
    """Looser than the memory usable gate. Used only while Finding."""
    if float(result.get("brightness") or 0) < float(getattr(config, "find_min_brightness", 12.0)):
        return False
    if float(result.get("blur_score") or 0) < float(getattr(config, "find_min_sharpness", 1.0)):
        return False
    rotation = gyro_dps(imu)
    limit = float(getattr(config, "find_max_gyro_dps", 240.0))
    if rotation is not None and rotation > limit:
        return False
    return True


def skin_mask(image: np.ndarray) -> np.ndarray:
    """Skin-coloured pixels only (no low-contrast full-frame cover)."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    skin = cv2.inRange(hsv, (0, 30, 40), (30, 200, 255))
    skin |= cv2.inRange(hsv, (160, 30, 40), (179, 200, 255))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, kernel)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, kernel)
    return skin


def occlusion_mask(image: np.ndarray) -> np.ndarray:
    """Bytes: 255 where a finger or flat cover is likely, 0 where the scene is likely visible.

    Tuned on the worn-ring recordings: a finger close to the lens is a large,
    soft skin-coloured region; a hand over the whole lens is nearly one colour.
    Soft textured scenes must not be wiped out — an earlier draft marked whole
    low-contrast rooms as covered and broke matching.
    """
    height, width = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    contrast = float(gray.std())
    # Full lens cover / facing a wall of one colour.
    if contrast < 10.0:
        return np.full((height, width), 255, dtype=np.uint8)
    covered = skin_mask(image)
    # Ignore speckles: a useful mask covers a real fraction of the picture.
    share = float(covered.mean()) / 255.0
    if share < 0.12:
        return np.zeros((height, width), dtype=np.uint8)
    return covered


def mask_finger_jpeg(jpeg: bytes, finger: np.ndarray | None = None) -> bytes:
    """Return a JPEG with finger / cover pixels set to black (for reconstruction export)."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return jpeg
    if finger is None:
        finger = cv2.bitwise_or(occlusion_mask(image), skin_mask(image))
    if finger.shape[:2] != image.shape[:2]:
        finger = cv2.resize(finger, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_NEAREST)
    masked = image.copy()
    masked[finger > 0] = 0
    ok, encoded = cv2.imencode(".jpg", masked, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    return encoded.tobytes() if ok else jpeg


def gyro_dps(imu: dict | None) -> float | None:
    if not imu or not imu.get("scaled"):
        return None
    return math.sqrt(imu["gx_dps"] ** 2 + imu["gy_dps"] ** 2 + imu["gz_dps"] ** 2)


def observe_ok(result: dict, config) -> str:
    """'' when this frame should enter continuous observation memory, else a short reason to skip it.

    Observations are kept even when they are blurry or partly covered. Only
    pictures that carry almost no light at all, or that cannot be decoded, are
    skipped, so the ring buffer is not filled with pure black frames.
    """
    if result["brightness"] < config.observation_min_brightness:
        return "dark"
    if result["brightness"] > config.max_brightness:
        return "bright"
    return ""


def landmark_ok(result: dict, imu: dict | None, config) -> str:
    """'' when this observation is clear enough to be a landmark for matching."""
    features = result.get("visible_feature_count", result["feature_count"])
    if result["occlusion"] >= config.landmark_max_occlusion:
        return "covered"
    if result["brightness"] < config.min_brightness:
        return "dark"
    if result["brightness"] > config.max_brightness:
        return "bright"
    if features < config.min_features:
        if result["contrast"] < config.obstructed_contrast:
            return "obstructed"
        return "blurry" if result["blur_score"] < config.min_sharpness else "few_features"
    if result["blur_score"] < config.min_sharpness:
        return "blurry"
    if result["coverage"] < config.min_coverage:
        return "covered"
    rotation = gyro_dps(imu)
    if rotation is not None and rotation > config.max_gyro_dps:
        return "fast_motion"
    return ""


# Kept for older call sites and tests that still say "judge".
def judge(result: dict, imu: dict | None, config) -> str:
    return landmark_ok(result, imu, config)


def match_keypoints(result: dict):
    """Keypoints used for matching: prefer the visible (unoccluded) set when the cover is partial."""
    occlusion = float(result.get("occlusion", 0.0))
    visible = result.get("visible_keypoints")
    descriptors = result.get("visible_descriptors")
    if 0.12 <= occlusion <= 0.85 and visible and descriptors is not None and len(visible) >= 8:
        return visible, descriptors
    return result.get("keypoints") or [], result.get("descriptors")


def compare(query_kp, query_desc, ref_kp, ref_desc, min_spread_px: float) -> dict:
    """Verified agreement between two pictures.

    inliers is the number of matched points that fit one homography. It is set
    to 0 when those points sit in one small clump on either picture, because a
    clump can fit a homography by chance. shift_px is how far the agreeing
    points moved between the two pictures, in pixels of the picture, not in the room.
    """
    if query_desc is None or ref_desc is None or len(query_kp) < 4 or len(ref_kp) < 4:
        return {
            "inliers": 0,
            "raw_inliers": 0,
            "matches": 0,
            "spread_px": 0.0,
            "shift_px": 0.0,
            "clumped": False,
        }
    compared = match_descriptors(query_kp, query_desc, ref_kp, ref_desc)
    points = [pair for pair in compared["pairs"] if pair["inlier"]]
    inliers = compared["inliers"]
    spread = 0.0
    shift = 0.0
    if points:
        query = np.float32([(pair["qx"], pair["qy"]) for pair in points])
        reference = np.float32([(pair["rx"], pair["ry"]) for pair in points])
        spread = float(min(np.sqrt(query.var(axis=0).sum()), np.sqrt(reference.var(axis=0).sum())))
        shift = float(np.median(np.linalg.norm(query - reference, axis=1)))
    clumped = inliers > 0 and spread < min_spread_px
    return {
        "inliers": 0 if clumped else inliers,
        "raw_inliers": inliers,
        "matches": compared["matches"],
        "spread_px": round(spread, 1),
        "shift_px": round(shift, 1),
        "clumped": clumped,
    }


def compare_results(query: dict, reference: dict, min_spread_px: float) -> dict:
    """Compare two analysed frames using visible-region keypoints when available."""
    q_kp, q_desc = match_keypoints(query)
    r_kp, r_desc = match_keypoints(reference)
    return compare(q_kp, q_desc, r_kp, r_desc, min_spread_px)


def duplicate_of(result: dict, recent, config) -> dict | None:
    """The recently kept landmark that shows nearly the same view as this one, or None."""
    q_kp, q_desc = match_keypoints(result)
    if q_desc is None:
        return None
    for kept in reversed(list(recent)):
        k_kp = kept.get("visible_keypoints") or kept.get("keypoints")
        k_desc = kept.get("visible_descriptors") if kept.get("visible_descriptors") is not None else kept.get("descriptors")
        if k_desc is None or not k_kp:
            continue
        compared = compare(q_kp, q_desc, k_kp, k_desc, config.min_inlier_spread_px)
        if compared["inliers"] >= config.duplicate_inliers and compared["shift_px"] < config.duplicate_shift_px:
            return kept
    return None


def view_blocked(result: dict, config) -> bool:
    """True when the live view is too covered to update Find progress."""
    return result.get("occlusion", 0.0) >= config.find_block_occlusion


def frame_usable(result: dict, imu: dict | None, config) -> tuple[bool, str]:
    """Base quality gate (brightness / blur / motion). Finger class is applied separately.

    Returns (usable, reject_reason). reject_reason is empty when usable is True.
    Thresholds are independent of the ORB landmark gates so Save/Find stay as they are.
    """
    if result["brightness"] < config.usable_min_brightness:
        return False, "too_dark"
    if result["blur_score"] < config.usable_min_sharpness:
        return False, "too_blurry"
    if result.get("occlusion", 0.0) > config.usable_max_occlusion:
        return False, "occlusion"
    rotation = gyro_dps(imu)
    if rotation is not None and rotation > config.usable_max_gyro_dps:
        return False, "motion"
    return True, ""


def classify_frame(result: dict, imu: dict | None, config, in_event_window: bool) -> tuple[str, bool, str]:
    """Return (frame_class, bank_eligible, reject_reason).

    frame_class:
      * "scene" — finger fraction under the scene cap; only class allowed into the keyframe bank
      * "event" — higher finger fraction, kept only inside grasp/release windows
      * "" — not a scene/event keeper for mapping
    """
    usable, reason = frame_usable(result, imu, config)
    finger = float(result.get("finger_fraction", result.get("occlusion", 0.0)))
    if not usable:
        return "", False, reason
    if finger <= config.scene_max_finger_fraction:
        return "scene", True, ""
    if finger <= config.event_max_finger_fraction and in_event_window:
        return "event", False, ""
    if finger > config.event_max_finger_fraction:
        return "", False, "finger"
    return "", False, "finger"  # between scene and event caps, outside a window
