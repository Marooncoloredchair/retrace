"""Every limit and threshold of the ring service in one place.

Defaults can be changed in ring_config.json next to retrace_ring.py, for example
    {"memory_max_frames": 600, "data_dir": "D:/retrace_memory", "find_backend": "orb"}
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

FIRMWARE_PULSE_MS = 200
FIRMWARE_REST_MS = 800
FIRMWARE_TICK_PULSE_MS = 80
FIRMWARE_TICK_REST_MS = 220


@dataclass
class Config:
    # Capture
    process_fps: float = 10.0  # frames analysed per second
    stall_seconds: float = 5.0
    reconnect_seconds: float = 1.0
    data_dir: str = ""  # empty = recordings/ring_memory under the project; set to move memory to another drive

    # Continuous observation memory
    observation_gap_s: float = 0.25
    observation_min_brightness: float = 8.0
    memory_max_frames: int = 1800
    memory_max_minutes: float = 60.0
    memory_max_mb: float = 250.0
    landmark_max_frames: int = 600
    imu_log_max_mb: float = 40.0
    imu_log_file_minutes: float = 5.0

    # Frame usability (groundwork for mapping) — separate from landmark ORB gates
    # usable_min_sharpness: Laplacian variance on grayscale resized to width 320.
    # 2.8 = floor of observed worn scores so blur does not reject during calibration runs;
    # pick the real cut from spaced contact sheets afterward.
    usable_min_brightness: float = 25.0
    usable_min_sharpness: float = 2.8
    usable_max_occlusion: float = 0.55
    usable_max_gyro_dps: float = 180.0

    # Host-side brightness lift (no firmware flash). Applied before analysis and export;
    # stored frame JPEGs remain the original camera bytes.
    host_brightness_lift: bool = True
    host_brightness_target_mean: float = 110.0
    host_brightness_max_gain: float = 5.0

    # Scene vs event (finger fraction = occlusion mask ∪ skin mask)
    scene_max_finger_fraction: float = 0.25
    event_max_finger_fraction: float = 0.60

    # DINOv2 keyframe bank (scene frames only)
    embedder_enabled: bool = True
    embedder_queue_size: int = 8
    keyframe_bank_max: int = 300
    keyframe_bank_window_s: float = 60.0
    keyframe_bank_min_distance: float = 0.15  # cosine distance vs recent bank keyframes

    # Hold / release (primary put-down detector; relative cover vs rolling baseline)
    # Legacy absolute thresholds kept for older configs; relative path is the default.
    hold_cover_high: float = 0.70
    hold_cover_low: float = 0.40
    hold_baseline_window_s: float = 30.0
    hold_gyro_max_dps: float = 90.0
    hold_rise_above_baseline: float = 0.2
    hold_return_within_baseline: float = 0.1
    hold_grasp_rise_s: float = 1.0  # must stay elevated this long to confirm grasp
    hold_min_s: float = 1.0  # unused by relative detector; kept for config compat
    hold_cooldown_s: float = 5.0  # at most one candidate per this many seconds
    hold_pre_grasp_s: float = 2.0
    hold_post_release_s: float = 3.0
    hold_pre_frames: int = 3
    hold_mid_frames: int = 2
    hold_post_frames: int = 4

    # VLM naming after RELEASED ("auto" picks anthropic/openai from env keys)
    vlm_provider: str = "auto"  # "auto", "openai", "anthropic", or "none"
    vlm_model: str = ""
    vlm_queue_size: int = 2

    # Legacy IMU put-down (secondary; Mark P remains ground truth)
    putdown_reach_accel_g: float = 0.45
    putdown_reach_max_s: float = 1.5
    putdown_still_accel_g: float = 0.12
    putdown_still_gyro_dps: float = 40.0
    putdown_still_min_ms: float = 150.0
    putdown_still_max_ms: float = 600.0
    putdown_withdraw_accel_g: float = 0.35
    putdown_cooldown_s: float = 1.5
    putdown_pin_before_s: float = 1.0
    putdown_pin_after_s: float = 3.0

    # Landmark index (ORB path)
    min_features: int = 40
    min_sharpness: float = 1.75
    min_coverage: float = 0.4
    min_brightness: float = 25.0
    max_brightness: float = 235.0
    max_gyro_dps: float = 220.0
    obstructed_contrast: float = 35.0
    landmark_max_occlusion: float = 0.55
    duplicate_inliers: int = 30
    duplicate_shift_px: float = 60.0
    duplicate_lookback: int = 3
    min_keyframe_gap_s: float = 0.4

    # Saved items
    max_items: int = 20
    save_lookback_s: float = 8.0
    save_capture_s: float = 6.0
    max_reference_frames: int = 8
    max_context_frames: int = 24
    trail_max_breadcrumbs: int = 200
    trail_max_minutes: float = 15.0
    crumb_gap_s: float = 0.5

    # Finding an item
    find_backend: str = "orb"  # "orb", "place", "pdr", or "map"
    find_map_min_pulse_interval: float = 0.3
    # Place-find (DINOv2 heat). ORB path ignores these.
    heat_span: float = 0.15
    place_pre_s: float = 25.0
    place_post_s: float = 25.0
    # Find-only gate. Looser than the memory usable gate. Finger pixels are cropped out first.
    find_min_brightness: float = 12.0
    find_min_sharpness: float = 1.0
    find_max_gyro_dps: float = 240.0
    place_heat_ema_s: float = 1.0
    place_pulse_cold_s: float = 2.5  # pulse interval at heat 0
    place_silence_s: float = 2.0  # no usable frame → silent / palm-out
    place_arrival_heat: float = 0.7
    place_arrival_inliers: int = 25
    place_arrival_fixes: int = 3
    place_arrival_window_s: float = 3.0
    place_aliked_max_keypoints: int = 1024
    # 'v' is an 80 ms pulse and 220 ms rest. 'V' stays 200 ms / 800 ms.
    firmware_tick_command: bool = True
    # Pedestrian dead reckoning (find_backend = "pdr"). Tunable by replaying the same IMU log.
    pdr_step_length: float = 0.7
    # Phone-in-pocket step length. Separate from the ring IMU's pdr_step_length.
    body_step_length: float = 0.75
    # A finished PDR test leaves pictures paused so the link stays up between tests.
    # The phone Resume camera button sends the pictures command.
    pdr_session_keep_camera_paused: bool = True
    # 0 uses each step's heading as the window computed it. Higher blends that
    # heading toward the previous step. It stays below 1 so a heading can still turn.
    pdr_heading_smooth: float = 0.0
    # "swing" uses the arm-swing axis. "gyro" integrates yaw from the first step.
    # "hybrid" follows yaw through turns and lets the swing axis trim drift.
    # The default stays "swing" until more loops say otherwise.
    pdr_heading_method: str = "swing"
    pdr_madgwick_beta: float = 0.08
    pdr_still_s: float = 0.5
    pdr_still_gyro_dps: float = 12.0
    pdr_window_s: float = 2.0
    pdr_hop_s: float = 0.5
    pdr_swing_confidence: float = 2.5
    # Horizontal std (g) below this is noise, not an arm swing, even if PCA looks one-dimensional.
    pdr_swing_min_std_g: float = 0.08
    # Vertical std (g) below this is not a footstep impact.
    pdr_step_min_std_g: float = 0.05
    pdr_swing_hz_lo: float = 0.6
    pdr_swing_hz_hi: float = 1.6
    pdr_step_hz_lo: float = 1.4
    pdr_step_hz_hi: float = 2.5
    # Walking gate on world-vertical acceleration. Scores start once
    # pdr_gait_min_span_s of vertical acceleration is in hand, looking back at
    # most pdr_gait_window_s. pdr_gait_start_windows passing hops open the gate
    # (about 1–1.5 s of rhythm at the 0.5 s hop). A miss shorter than
    # pdr_gait_stop_s does not close the gate or wipe that streak, so a turn
    # can continue. Swing and carry are both blocked outside the gate.
    # The band-power ratio is what rejects sitting hand motion.
    pdr_gait_window_s: float = 2.0
    pdr_gait_min_span_s: float = 1.0
    # Vertical std below this is a finger wiggle, not a step, even if the
    # spectrum happens to sit in the step band.
    pdr_gait_min_std_g: float = 0.18
    # A 2 s window often misses the autocorrelation peak. Band power at or
    # above this, with real vertical motion, is enough on its own.
    pdr_gait_band_strong: float = 0.55
    pdr_gait_acf_min: float = 0.12
    pdr_gait_lag_lo_s: float = 0.4
    pdr_gait_lag_hi_s: float = 1.2
    pdr_gait_band_min: float = 0.50
    pdr_gait_wide_lo_hz: float = 0.3
    pdr_gait_wide_hi_hz: float = 8.0
    pdr_gait_band_lo_hz: float = 0.7
    pdr_gait_band_hi_hz: float = 2.5
    pdr_gait_start_windows: int = 2
    pdr_gait_stop_s: float = 2.0
    pdr_yaw_flip_deg: float = 120.0
    pdr_amplitude_correction: bool = False
    pdr_amplitude_gain: float = 0.0
    pdr_heat_range_m: float = 6.0
    pdr_close_m: float = 1.0
    pdr_visual_correction: bool = False
    pdr_correction_gain: float = 0.5
    pdr_correction_max_m: float = 4.0
    pdr_correction_window_s: float = 3.0
    # Cosine distance at or below this counts as a confident place match to a put-down ladder frame.
    pdr_correction_max_distance: float = 0.35
    min_inliers: int = 12
    inlier_margin: int = 8
    min_inlier_spread_px: float = 18.0
    window_frames: int = 10
    window_seconds: float = 4.0
    required_frames: int = 3
    arrival_required_frames: int = 3
    rival_ratio: float = 0.4
    neighbour_stages: int = 3
    same_scene_cap: int = 20
    conflict_stages: int = 8
    jump_stages: int = 12
    jump_required_frames: int = 5
    jump_window_s: float = 5.0
    progress_step: int = 4
    find_max_minutes: float = 10.0
    match_threads: int = 4
    find_block_occlusion: float = 0.7

    # Motor
    motor_enabled: bool = True
    motor_min_interval_s: float = 3.0
    motor_max_per_minute: int = 8

    # Web
    port: int = 8010
    # "serial" is the USB port. "wifi" listens for the ring on ring_wifi_port.
    ring_transport: str = "serial"
    ring_wifi_port: int = 8011
    # When true, the dashboard also answers on the LAN. The phone page needs the token.
    dashboard_lan: bool = False

    def validate(self) -> None:
        floor = (FIRMWARE_PULSE_MS + FIRMWARE_REST_MS) / 1000.0 + 0.2
        if self.motor_min_interval_s < floor:
            raise ValueError(f"motor_min_interval_s must be at least {floor} s, the firmware's pulse plus rest time.")
        if self.motor_max_per_minute < 0 or self.motor_max_per_minute > 30:
            raise ValueError("motor_max_per_minute must be between 0 and 30.")
        if self.required_frames < 2 or self.arrival_required_frames < 2:
            raise ValueError("A single frame must never confirm a match. Use 2 or more required frames.")
        if self.required_frames > self.window_frames or self.jump_required_frames > self.window_frames:
            raise ValueError("Required frames cannot exceed the window.")
        if self.min_inliers < 8:
            raise ValueError("min_inliers below 8 lets a homography fit almost anything.")
        if not 0 < self.rival_ratio <= 1:
            raise ValueError("rival_ratio must be above 0 and at most 1.")
        if self.duplicate_lookback < 1:
            raise ValueError("duplicate_lookback must be at least 1.")
        for name in ("memory_max_frames", "memory_max_minutes", "memory_max_mb", "max_items", "trail_max_breadcrumbs"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be above zero so storage stays bounded.")
        if self.min_features < self.min_inliers:
            raise ValueError("min_features below min_inliers would keep landmarks that can never be matched.")
        if not 0 < self.process_fps <= 30:
            raise ValueError("process_fps must be between 0 and 30.")
        if not 0 <= self.landmark_max_occlusion <= 1 or not 0 <= self.find_block_occlusion <= 1:
            raise ValueError("occlusion limits must be between 0 and 1.")
        if self.find_backend not in ("orb", "map", "place", "pdr"):
            raise ValueError('find_backend must be "orb", "place", "pdr", or "map".')
        if self.ring_transport not in ("serial", "wifi"):
            raise ValueError('ring_transport must be "serial" or "wifi".')
        if not 1 <= self.ring_wifi_port <= 65535:
            raise ValueError("ring_wifi_port must be a TCP port.")
        if self.pdr_step_length <= 0:
            raise ValueError("pdr_step_length must be above zero.")
        if self.body_step_length <= 0:
            raise ValueError("body_step_length must be above zero.")
        if not 0 <= self.pdr_heading_smooth < 1:
            raise ValueError("pdr_heading_smooth must be in [0, 1).")
        if self.pdr_heading_method not in ("swing", "gyro", "hybrid"):
            raise ValueError('pdr_heading_method must be "swing", "gyro", or "hybrid".')
        if not 0 < self.pdr_gait_acf_min <= 1 or not 0 < self.pdr_gait_band_min <= 1:
            raise ValueError("pdr gait autocorrelation and band-power thresholds must be in (0, 1].")
        if self.pdr_gait_lag_lo_s <= 0 or self.pdr_gait_lag_lo_s >= self.pdr_gait_lag_hi_s:
            raise ValueError("pdr gait lag window must be a positive range.")
        if self.pdr_gait_start_windows < 1 or self.pdr_gait_stop_s <= 0 or self.pdr_gait_window_s <= 0:
            raise ValueError("PDR gait window, start count, and stop time must be positive.")
        if self.pdr_gait_min_span_s <= 0 or self.pdr_gait_min_span_s > self.pdr_gait_window_s:
            raise ValueError("pdr_gait_min_span_s must be positive and no longer than the gait window.")
        if self.pdr_gait_min_std_g <= 0:
            raise ValueError("pdr_gait_min_std_g must be above zero.")
        if not self.pdr_gait_band_min <= self.pdr_gait_band_strong <= 1:
            raise ValueError("pdr_gait_band_strong must be between the band minimum and 1.")
        tick_floor = (FIRMWARE_TICK_PULSE_MS + FIRMWARE_TICK_REST_MS) / 1000.0
        if self.firmware_tick_command and self.find_map_min_pulse_interval < tick_floor:
            raise ValueError("find_map_min_pulse_interval is shorter than the firmware tick pulse+rest.")
        if self.heat_span <= 0:
            raise ValueError("heat_span must be above zero.")
        if not 0 < self.place_arrival_heat <= 1:
            raise ValueError("place_arrival_heat must be in (0, 1].")
        if self.putdown_still_min_ms >= self.putdown_still_max_ms:
            raise ValueError("putdown_still_min_ms must be below putdown_still_max_ms.")
        if self.keyframe_bank_max < 1:
            raise ValueError("keyframe_bank_max must be at least 1.")
        if not 0 <= self.scene_max_finger_fraction < self.event_max_finger_fraction <= 1:
            raise ValueError("scene_max_finger_fraction must be below event_max_finger_fraction (both in 0..1).")
        if self.hold_cover_low >= self.hold_cover_high:
            raise ValueError("hold_cover_low must be below hold_cover_high.")
        if self.vlm_provider not in ("none", "auto", "openai", "anthropic"):
            raise ValueError('vlm_provider must be "none", "auto", "openai", or "anthropic".')
        if self.hold_rise_above_baseline <= 0 or self.hold_return_within_baseline < 0:
            raise ValueError("hold rise/return thresholds must be non-negative (rise > 0).")
        if self.hold_cooldown_s < 0:
            raise ValueError("hold_cooldown_s must be non-negative.")

    def public(self) -> dict:
        return asdict(self)


def load_config(path: Path | None) -> Config:
    config = Config()
    if path is not None and path.is_file():
        values = json.loads(path.read_text(encoding="utf-8"))
        known = {field.name for field in fields(Config)}
        for key, value in values.items():
            if key not in known:
                raise ValueError(f"{path.name}: unknown setting {key!r}.")
            current = getattr(config, key)
            if isinstance(current, bool):
                if not isinstance(value, bool):
                    raise ValueError(f"{path.name}: {key} must be true or false.")
                setattr(config, key, value)
            elif isinstance(current, str):
                if not isinstance(value, str):
                    raise ValueError(f"{path.name}: {key} must be a string.")
                setattr(config, key, value)
            elif not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{path.name}: {key} must be a number.")
            else:
                setattr(config, key, type(current)(value))
    config.validate()
    return config
