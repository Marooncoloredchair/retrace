"""Pedestrian dead reckoning from the ring MPU6050 (6-axis, no magnetometer).

World frame is gravity-aligned. Yaw comes from bias-corrected gyro.
Walking direction is the first principal axis of horizontal acceleration
over a swing window (arm-swing PDR), unless pdr_heading_method says otherwise.
One swing cycle counts as two steps.

The live IMU is 100 Hz (10 ms, from the sample counter). Windows and the
still detector are in seconds, so a replay of an older ~28 Hz log still runs.
A gap larger than 250 ms is not integrated.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np


def _norm(v: np.ndarray) -> float:
    return float(np.linalg.norm(v)) or 1.0


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        dtype=np.float64,
    )


def _rotate(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Rotate a 3-vector by quaternion q (w, x, y, z)."""
    packed = np.array([0.0, v[0], v[1], v[2]])
    conj = np.array([q[0], -q[1], -q[2], -q[3]])
    out = _quat_mul(_quat_mul(q, packed), conj)
    return out[1:]


def _yaw_from_quat(q: np.ndarray) -> float:
    w, x, y, z = q
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def _wrap(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


# A turn-around is the gyro's net yaw change over this window, not the
# change since the last hop. A gradual U-turn never exceeds the flip
# threshold in one hop, and it does over four seconds.
TURN_WINDOW_S = 4.0
HYBRID_FLIP_WINDOW_S = TURN_WINDOW_S
HYBRID_DRIFT_GAIN = 0.2
HYBRID_DRIFT_MAX_RAD = math.radians(60.0)
# std of sample-to-sample yaw, radians. A smooth arm swing sits near 0.01.
# Handling the ring to press the button is several tenths.
HANDLING_GYRO_STD = 0.20
HEADING_METHODS = ("swing", "gyro", "hybrid")


def madgwick(q: np.ndarray, gyro: np.ndarray, acc: np.ndarray, dt: float, beta: float) -> np.ndarray:
    """One 6-axis Madgwick step. gyro in rad/s, acc in g, q is wxyz."""
    if dt <= 0:
        return q
    q0, q1, q2, q3 = q
    gx, gy, gz = gyro
    a_norm = _norm(acc)
    ax, ay, az = acc / a_norm
    f = np.array(
        [
            2.0 * (q1 * q3 - q0 * q2) - ax,
            2.0 * (q0 * q1 + q2 * q3) - ay,
            2.0 * (0.5 - q1 * q1 - q2 * q2) - az,
        ]
    )
    j = np.array(
        [
            [-2 * q2, 2 * q3, -2 * q0, 2 * q1],
            [2 * q1, 2 * q0, 2 * q3, 2 * q2],
            [0.0, -4 * q1, -4 * q2, 0.0],
        ]
    )
    step = j.T @ f
    step_n = _norm(step)
    step = step / step_n
    q_dot = 0.5 * _quat_mul(q, np.array([0.0, gx, gy, gz])) - beta * step
    q_new = q + q_dot * dt
    return q_new / _norm(q_new)


def gait_scores(vert: np.ndarray, dt: float, config) -> tuple[float, float]:
    """Normalized autocorrelation peak and in-band power fraction of vertical acceleration.

    The peak is the strongest autocorrelation at a lag inside the step period.
    The ratio is power in the step band divided by power from the wide band.
    """
    if len(vert) < 16 or dt <= 0:
        return 0.0, 0.0
    centered = vert - float(np.mean(vert))
    energy = float(np.dot(centered, centered))
    if energy < 1e-8:
        return 0.0, 0.0
    lag_lo = max(1, int(round(float(config.pdr_gait_lag_lo_s) / dt)))
    lag_hi = min(len(centered) - 2, int(round(float(config.pdr_gait_lag_hi_s) / dt)))
    peak = 0.0
    if lag_hi > lag_lo:
        corr = np.correlate(centered, centered, mode="full")
        mid = len(centered) - 1
        peak = float(np.max(corr[mid + lag_lo : mid + lag_hi + 1]) / energy)

    spec = np.abs(np.fft.rfft(centered)) ** 2
    freqs = np.fft.rfftfreq(len(centered), d=dt)
    wide = (freqs >= float(config.pdr_gait_wide_lo_hz)) & (freqs <= float(config.pdr_gait_wide_hi_hz))
    band = (freqs >= float(config.pdr_gait_band_lo_hz)) & (freqs <= float(config.pdr_gait_band_hi_hz))
    wide_power = float(np.sum(spec[wide]))
    ratio = float(np.sum(spec[band]) / wide_power) if wide_power > 0 else 0.0
    return peak, ratio


def smooth_heading(previous: np.ndarray | None, heading: np.ndarray, alpha: float) -> np.ndarray:
    """Blend this step's heading toward the previous step. alpha 0 leaves it unchanged."""
    if previous is None or alpha <= 0:
        return heading
    mixed = (1.0 - alpha) * heading + alpha * previous
    scale = float(np.linalg.norm(mixed))
    if scale < 1e-8:
        return heading
    return mixed / scale


def _band_spectrum(signal: np.ndarray, dt: float, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray]:
    """(frequency, power) of the rFFT bins inside [lo, hi]. Empty if the window is too short."""
    n = len(signal)
    if n < 8 or dt <= 0:
        return np.zeros(0), np.zeros(0)
    centered = signal - float(np.mean(signal))
    spec = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(n, d=dt)
    mask = (freqs >= lo) & (freqs <= hi)
    if not np.any(mask):
        return np.zeros(0), np.zeros(0)
    return freqs[mask], spec[mask]


def _dominant_hz(signal: np.ndarray, dt: float, lo: float, hi: float) -> tuple[float, float]:
    """Return (frequency Hz, power) of the strongest bin inside [lo, hi]."""
    freqs, power = _band_spectrum(signal, dt, lo, hi)
    if len(freqs) == 0:
        return 0.0, 0.0
    index = int(np.argmax(power))
    return float(freqs[index]), float(power[index])


def _count_new_peaks(times: np.ndarray, vert: np.ndarray, dt: float, t_lo: float) -> int:
    """Footstep peaks in (t_lo, now]. One peak is one step.

    The window is band-limited to the step band so a harmonic of the arm
    swing does not add a second peak. Peaks before t_lo belong to an earlier hop.
    """
    n = len(vert)
    if n < 16 or dt <= 0:
        return 0
    centered = vert - float(np.mean(vert))
    spec = np.fft.rfft(centered)
    freqs = np.fft.rfftfreq(n, d=dt)
    spec = spec.copy()
    spec[(freqs < 0.7) | (freqs > 2.5)] = 0.0
    wave = np.fft.irfft(spec, n=n)
    scale = float(np.std(wave))
    if scale < 1e-4:
        return 0
    thr = 0.35 * scale
    count = 0
    last = -1e9
    for index in range(1, n - 1):
        when = float(times[index])
        if when <= t_lo:
            continue
        if wave[index] > wave[index - 1] and wave[index] >= wave[index + 1] and wave[index] > thr and when - last >= 0.35:
            count += 1
            last = when
    return count


def _fundamental_hz(signal: np.ndarray, dt: float, lo: float, hi: float) -> tuple[float, float]:
    """Lowest strong bin in [lo, hi]. A higher harmonic is not the arm-swing cycle."""
    freqs, power = _band_spectrum(signal, dt, lo, hi)
    if len(freqs) == 0:
        return 0.0, 0.0
    peak = float(np.max(power))
    if peak <= 0:
        return 0.0, 0.0
    # Half the peak is still the cycle. Anything weaker is a side lobe.
    for freq, mag in zip(freqs, power):
        if float(mag) >= 0.5 * peak:
            return float(freq), float(mag)
    return float(freqs[int(np.argmax(power))]), peak


def features_from_window(win, config) -> dict | None:
    """Stateless hop measurements. Axis sign is resolved later, so this can be cached."""
    if not win:
        return None
    t_now = float(win[-1][0])
    pca = [row for row in win if t_now - row[0] <= float(config.pdr_window_s)]
    if len(pca) < 12:
        return None
    times = np.array([row[0] for row in pca])
    horiz = np.array([[row[1], row[2]] for row in pca])
    vert = np.array([row[3] for row in pca])
    yaw = np.array([row[4] for row in pca])
    dt = float(np.median(np.diff(times))) if len(times) > 2 else 1.0 / 28.0
    duration = float(times[-1] - times[0])
    if duration < 0.4:
        return None
    centered = horiz - horiz.mean(axis=0)
    cov = centered.T @ centered / max(len(centered), 1)
    vals, vecs = np.linalg.eigh(cov)
    axis = vecs[:, -1]
    proj = centered @ axis
    swing_hz, _swing_pow = _fundamental_hz(
        proj, dt, float(config.pdr_swing_hz_lo), float(config.pdr_swing_hz_hi)
    )
    # A bin that lands in the step band is the two-steps-per-cycle harmonic.
    if swing_hz >= float(config.pdr_step_hz_lo):
        swing_hz *= 0.5
    step_hz, step_pow = _fundamental_hz(vert, dt, float(config.pdr_step_hz_lo), float(config.pdr_step_hz_hi))
    gait_rows = [row for row in win if t_now - row[0] <= float(config.pdr_gait_window_s)]
    gait_t = np.array([row[0] for row in gait_rows])
    gait_z = np.array([row[3] for row in gait_rows])
    gait_dt = float(np.median(np.diff(gait_t))) if len(gait_t) > 2 else dt
    span = float(gait_t[-1] - gait_t[0]) if len(gait_t) > 1 else 0.0
    if span >= float(config.pdr_gait_min_span_s):
        acf_peak, band_ratio = gait_scores(gait_z, gait_dt, config)
    else:
        acf_peak, band_ratio = 0.0, 0.0
    gyro_std = float(np.std(np.diff(yaw))) if len(yaw) > 3 else 0.0
    step_peaks = _count_new_peaks(times, vert, dt, t_now - float(config.pdr_hop_s))
    return {
        "t": t_now,
        "axis": (float(axis[0]), float(axis[1])),
        "confidence": float(vals[-1] / max(float(vals[0]), 1e-8)),
        "yaw": float(yaw[-1]),
        "swing_hz": float(swing_hz),
        "horiz_std": float(np.std(centered)),
        "proj_std": float(np.std(proj)),
        "step_hz": float(step_hz),
        "vert_std": float(np.std(vert)),
        "step_pow": float(step_pow),
        "gyro_std": gyro_std,
        "acf": float(acf_peak),
        "band": float(band_ratio),
        "duration": duration,
        "step_peaks": int(step_peaks),
    }


def world_samples(rows: list[dict], config) -> list[tuple]:
    """World-frame rows a tracker would append, without taking any steps."""
    tracker = PdrTracker(config)
    series = []
    for row in rows:
        got = tracker.ingest(row)
        if got is not None:
            series.append(got)
    return series


def hop_features(series: list[tuple], config) -> list[dict]:
    """One feature row per analysis hop. Independent of the gait and heading decisions."""
    win: deque = deque()
    horizon = max(float(config.pdr_window_s), float(config.pdr_gait_window_s)) + 0.5
    next_hop = float(config.pdr_hop_s)
    hops = []
    for sample in series:
        t = sample[0]
        win.append(sample)
        while win and t - win[0][0] > horizon:
            win.popleft()
        if t >= next_hop:
            next_hop = t + float(config.pdr_hop_s)
            feats = features_from_window(win, config)
            if feats is not None:
                hops.append(feats)
    return hops


@dataclass
class PdrSampleOut:
    t: float
    x: float
    y: float
    yaw: float
    mode: str
    steps: float
    confidence: float
    axis_x: float
    axis_y: float
    acf_peak: float = 0.0
    band_ratio: float = 0.0
    walking: bool = False


@dataclass
class PdrTracker:
    config: object
    q: np.ndarray = field(default_factory=lambda: np.array([1.0, 0.0, 0.0, 0.0]))
    bias: np.ndarray = field(default_factory=lambda: np.zeros(3))
    pos: np.ndarray = field(default_factory=lambda: np.zeros(2))
    yaw: float = 0.0
    mode: str = "still"
    confidence: float = 0.0
    axis: np.ndarray = field(default_factory=lambda: np.array([1.0, 0.0]))
    steps: float = 0.0
    path_m: float = 0.0
    last_heading: float = 0.0
    last_confident_yaw: float = 0.0
    have_confident: bool = False
    walking: bool = False
    acf_peak: float = 0.0
    band_ratio: float = 0.0
    _gait_streak: int = 0
    _gait_fail_t: float | None = None
    _rhythm_began: float | None = None
    _credited_through: float = -1.0
    _recent: deque = field(default_factory=deque)
    gate_events: list = field(default_factory=list)
    t0: float | None = None
    t: float = 0.0
    _last_t: float | None = None
    _gyro_buf: deque = field(default_factory=deque)
    _win: deque = field(default_factory=deque)
    _next_hop: float = 0.0
    _smooth_heading: np.ndarray | None = None
    _gyro_origin: float | None = None
    _hybrid_heading: float | None = None
    _prev_hop_yaw: float | None = None
    _yaw_hist: deque = field(default_factory=deque)
    _commit_log: list | None = None
    trace: list = field(default_factory=list)
    marks: list = field(default_factory=list)
    corrections: list = field(default_factory=list)

    def reset(self) -> None:
        self.q = np.array([1.0, 0.0, 0.0, 0.0])
        self.bias[:] = 0.0
        self.pos[:] = 0.0
        self.yaw = 0.0
        self.mode = "still"
        self.steps = 0.0
        self.path_m = 0.0
        self.axis = np.array([1.0, 0.0])
        self.last_heading = 0.0
        self.last_confident_yaw = 0.0
        self.confidence = 0.0
        self.have_confident = False
        self.walking = False
        self.acf_peak = 0.0
        self.band_ratio = 0.0
        self._gait_streak = 0
        self._gait_fail_t = None
        self._rhythm_began = None
        self._credited_through = -1.0
        self._recent.clear()
        self.gate_events.clear()
        self._smooth_heading = None
        self._gyro_origin = None
        self._hybrid_heading = None
        self._prev_hop_yaw = None
        self._yaw_hist.clear()
        self.t0 = None
        self.t = 0.0
        self._last_t = None
        self._next_hop = 0.0
        self._gyro_buf.clear()
        self._win.clear()
        self.trace.clear()
        self.marks.clear()
        self.corrections.clear()
        if self._commit_log is not None:
            self._commit_log.clear()

    def push(self, sample: dict) -> None:
        if getattr(self, "frozen", False):
            return
        got = self.ingest(sample)
        if got is None:
            return
        self._win.append(got)
        horizon = max(float(self.config.pdr_window_s), float(self.config.pdr_gait_window_s)) + 0.5
        while self._win and self.t - self._win[0][0] > horizon:
            self._win.popleft()
        if self.t >= self._next_hop:
            self._next_hop = self.t + float(self.config.pdr_hop_s)
            self._step_window()

    def ingest(self, sample: dict) -> tuple | None:
        """Integrate one IMU sample. Returns the world-frame row the step window would see."""
        ax = sample.get("ax_g")
        gx = sample.get("gx_dps")
        if ax is None or gx is None:
            return None
        t = float(sample.get("host_unix") or sample.get("t") or 0.0)
        if self.t0 is None:
            self.t0 = t
            self._last_t = t
            self._next_hop = float(self.config.pdr_hop_s)
            return None
        dt = t - (self._last_t or t)
        # A Wi-Fi batch can carry an earlier timestamp than the sample already
        # integrated. Rewinding the clock made the gait gate open twice and
        # counted those steps again. Ignore the late sample and keep time moving
        # forward. A real gap still advances the clock so the next sample is kept.
        if dt <= 0:
            return None
        self._last_t = t
        if dt > 0.25:
            return None
        self.t = t - self.t0
        acc = np.array([sample["ax_g"], sample["ay_g"], sample["az_g"]], dtype=np.float64)
        gyro = np.radians(
            np.array([sample["gx_dps"], sample["gy_dps"], sample["gz_dps"]], dtype=np.float64)
        )
        self._update_bias(gyro, t)
        gyro = gyro - self.bias
        self.q = madgwick(self.q, gyro, acc, dt, float(self.config.pdr_madgwick_beta))
        self.yaw = _yaw_from_quat(self.q)
        world = _rotate(self.q, acc)
        linear = world - np.array([0.0, 0.0, 1.0])
        return (self.t, float(linear[0]), float(linear[1]), float(linear[2]), float(self.yaw))

    def _update_bias(self, gyro: np.ndarray, t: float) -> None:
        self._gyro_buf.append((t, gyro.copy()))
        keep = float(self.config.pdr_still_s)
        while self._gyro_buf and t - self._gyro_buf[0][0] > keep:
            self._gyro_buf.popleft()
        if len(self._gyro_buf) < 8:
            return
        span = self._gyro_buf[-1][0] - self._gyro_buf[0][0]
        if span < keep * 0.9:
            return
        stack = np.stack([g for _, g in self._gyro_buf])
        if float(np.std(stack)) > math.radians(float(self.config.pdr_still_gyro_dps)):
            return
        # Slow bias update while still.
        self.bias = 0.9 * self.bias + 0.1 * np.mean(stack, axis=0)

    def _step_window(self) -> None:
        feats = features_from_window(self._win, self.config)
        if feats is None:
            return
        self.apply_hop(feats)

    def _gyro_heading(self, yaw_now: float) -> np.ndarray:
        """Walking direction is yaw relative to the yaw at the first step."""
        if self._gyro_origin is None:
            self._gyro_origin = float(yaw_now)
        heading = _wrap(float(yaw_now) - self._gyro_origin)
        return np.array([math.cos(heading), math.sin(heading)])

    def _hybrid_heading_vec(self, raw_axis: np.ndarray, yaw_now: float, correct: bool) -> np.ndarray:
        """Gyro takes the turn. The swing axis only pulls off slow drift.

        The axis may be flipped 180° only when the net yaw change over the
        last four seconds exceeds pdr_yaw_flip_deg. Otherwise an opposed
        axis is ignored, so drift cannot reverse the walker.
        """
        t = float(self.t)
        yaw_now = float(yaw_now)
        if self._prev_hop_yaw is None or self._hybrid_heading is None:
            self._prev_hop_yaw = yaw_now
            ang = math.atan2(float(raw_axis[1]), float(raw_axis[0]))
            self._hybrid_heading = ang
            return np.array([math.cos(ang), math.sin(ang)])
        heading = _wrap(self._hybrid_heading + _wrap(yaw_now - self._prev_hop_yaw))
        self._prev_hop_yaw = yaw_now
        span = t - self._yaw_hist[0][0] if self._yaw_hist else 0.0
        delta_4s = _wrap(yaw_now - self._yaw_hist[0][1]) if span >= TURN_WINDOW_S - 0.5 else 0.0
        flip_ok = abs(delta_4s) > math.radians(float(self.config.pdr_yaw_flip_deg))
        axis = np.array(raw_axis, dtype=np.float64)
        pointing = np.array([math.cos(heading), math.sin(heading)])
        opposed = float(np.dot(axis, pointing)) < 0
        # An opposed axis would reverse the walker. Accept that 180° flip only
        # after the gyro itself has turned through the window.
        if opposed and flip_ok:
            axis = -axis
        if correct and not (opposed and not flip_ok):
            err = _wrap(math.atan2(float(axis[1]), float(axis[0])) - heading)
            if abs(err) <= HYBRID_DRIFT_MAX_RAD:
                heading = _wrap(heading + HYBRID_DRIFT_GAIN * err)
        self._hybrid_heading = heading
        return np.array([math.cos(heading), math.sin(heading)])

    def apply_hop(self, feats: dict) -> None:
        """Advance one analysis hop. `feats` comes from features_from_window."""
        self.t = float(feats["t"])
        raw_axis = np.array(feats["axis"], dtype=np.float64)
        axis = raw_axis.copy()
        # Continuity resolves the 180° PCA flip.
        if float(np.dot(axis, self.axis)) < 0:
            axis = -axis
        yaw_now = float(feats["yaw"])
        # Net yaw change over the last four seconds. The heading stored with
        # the old sample is the direction we believed then.
        self._yaw_hist.append((self.t, yaw_now, float(self.last_heading)))
        while self._yaw_hist and self.t - self._yaw_hist[0][0] > TURN_WINDOW_S:
            self._yaw_hist.popleft()
        span = self.t - self._yaw_hist[0][0]
        if self.have_confident and span >= TURN_WINDOW_S - 0.5:
            _t_then, yaw_then, heading_then = self._yaw_hist[0]
            yaw_turn = _wrap(yaw_now - float(yaw_then))
            if abs(yaw_turn) > math.radians(float(self.config.pdr_yaw_flip_deg)):
                turned = np.array(
                    [
                        math.cos(float(heading_then) + yaw_turn),
                        math.sin(float(heading_then) + yaw_turn),
                    ]
                )
                if float(np.dot(axis, turned)) < 0:
                    axis = -axis

        confidence = float(feats["confidence"])
        swing_hz = float(feats["swing_hz"])
        step_hz = float(feats["step_hz"])
        swing_ok = (
            confidence >= float(self.config.pdr_swing_confidence)
            and swing_hz > 0
            and float(feats["horiz_std"]) >= float(self.config.pdr_swing_min_std_g)
        )
        carry_ok = (
            (not swing_ok)
            and step_hz > 0
            and float(feats["vert_std"]) >= float(self.config.pdr_step_min_std_g)
            and float(feats["step_pow"]) > float(feats["vert_std"]) * 0.5
        )

        mode = "still"
        steps_now = 0.0
        heading_vec = axis
        acf_peak = float(feats["acf"])
        band_ratio = float(feats["band"])
        vert_std = float(feats["vert_std"])
        band_min = float(self.config.pdr_gait_band_min)
        # Classic test: a real autocorrelation and step-band power together.
        # Strong-band test: a short window often misses the autocorrelation
        # peak on a genuine walk. A strong step band plus real vertical
        # motion counts, which a small sitting wiggle does not have.
        classic = acf_peak >= float(self.config.pdr_gait_acf_min) and band_ratio >= band_min
        strong = (
            band_ratio >= float(self.config.pdr_gait_band_strong)
            and vert_std >= float(self.config.pdr_gait_min_std_g)
        )
        gait_ok = classic or strong
        # Handling the ring is wild yaw with no step rhythm (the stillness
        # beside it already contributes nothing). A turn-around keeps the
        # rhythm, so the same yaw change still counts as steps.
        handling = float(feats["gyro_std"]) >= HANDLING_GYRO_STD and not classic
        if "step_peaks" in feats:
            counted = float(feats["step_peaks"])
        else:
            counted = None
        if handling:
            mode = "still"
            gait_ok = False
        elif swing_ok:
            mode = "swing"
            # One arm-swing cycle is two steps. A peak in the step band is one
            # step, so the harmonic of the cycle is not counted again.
            if counted is not None:
                steps_now = counted
            else:
                duration = float(feats["duration"])
                steps_now = 2.0 * swing_hz * duration * (float(self.config.pdr_hop_s) / duration)
            heading_vec = axis
            self.axis = axis
            self.last_heading = math.atan2(axis[1], axis[0])
            self.last_confident_yaw = yaw_now
            self.have_confident = True
        elif carry_ok and self.have_confident:
            mode = "carry"
            if counted is not None:
                steps_now = counted
            else:
                steps_now = step_hz * float(self.config.pdr_hop_s)
            heading = self.last_heading + _wrap(yaw_now - self.last_confident_yaw)
            heading_vec = np.array([math.cos(heading), math.sin(heading)])
        elif float(feats["gyro_std"]) < math.radians(float(self.config.pdr_still_gyro_dps)) and float(
            feats["step_pow"]
        ) < 1.0:
            mode = "still"
            steps_now = 0.0

        method = getattr(self.config, "pdr_heading_method", "swing")
        if method == "gyro" and mode in ("swing", "carry"):
            heading_vec = self._gyro_heading(yaw_now)
        elif method == "hybrid":
            hybrid_vec = self._hybrid_heading_vec(raw_axis, yaw_now, correct=(mode == "swing"))
            if mode in ("swing", "carry"):
                heading_vec = hybrid_vec

        length = float(self.config.pdr_step_length)
        if self.config.pdr_amplitude_correction and swing_ok and not handling:
            length *= 1.0 + float(self.config.pdr_amplitude_gain) * float(feats["proj_std"])
        self.acf_peak = acf_peak
        self.band_ratio = band_ratio
        motion = {
            "steps": steps_now if mode != "still" else 0.0,
            "heading": np.array(heading_vec, dtype=np.float64, copy=True),
            "length": length,
            "gait_ok": bool(gait_ok),
            "erratic": handling,
        }
        self._update_walking(bool(gait_ok), motion)
        self.mode = mode
        self.confidence = confidence
        self.trace.append(
            PdrSampleOut(
                t=self.t,
                x=float(self.pos[0]),
                y=float(self.pos[1]),
                yaw=self.yaw,
                mode=mode if self.walking else "still",
                steps=self.steps,
                confidence=confidence,
                axis_x=float(axis[0]),
                axis_y=float(axis[1]),
                acf_peak=acf_peak,
                band_ratio=band_ratio,
                walking=self.walking,
            )
        )

    def _commit(self, motion: dict) -> None:
        steps_now = float(motion["steps"])
        if steps_now <= 0:
            return
        heading = np.array(motion["heading"], dtype=np.float64)
        if self._commit_log is not None:
            base = float(self.config.pdr_step_length) or 1.0
            unit = float(motion["length"]) / base
            self._commit_log.append((self.t, steps_now, float(heading[0]), float(heading[1]), unit))
        alpha = float(getattr(self.config, "pdr_heading_smooth", 0.0))
        heading = smooth_heading(self._smooth_heading, heading, alpha)
        self._smooth_heading = heading
        delta = heading * (steps_now * float(motion["length"]))
        self.pos = self.pos + delta
        self.path_m += float(np.linalg.norm(delta))
        self.steps += steps_now

    def _credit_confirming(self) -> None:
        """Commit the trailing hops that opened the gate, and nothing before them.

        The stretch before the rhythm is the stillness and the erratic handling
        around the mark. Those hops are not steps.
        """
        saved = self.t
        passed: list[tuple[float, dict]] = []
        for hop_t, stored in reversed(self._recent):
            if not stored.get("gait_ok") or stored.get("erratic"):
                break
            passed.append((hop_t, stored))
        for hop_t, stored in reversed(passed):
            if hop_t <= self._credited_through + 1e-9:
                continue
            self.t = hop_t
            self._commit(stored)
            self._credited_through = hop_t
        self.t = saved

    def _close_gate(self) -> None:
        if self.walking:
            self.gate_events.append(("close", round(self.t, 3)))
        self.walking = False
        self._rhythm_began = None
        self._gait_streak = 0
        self._gait_fail_t = None

    def _update_walking(self, gait_ok: bool, motion: dict) -> None:
        """Open after about 1–1.5 s of rhythm. Handling at the edges does not count.

        Opening credits the hops that confirmed the rhythm, not the still and
        erratic stretch before them. While the gate is open, a miss shorter
        than pdr_gait_stop_s keeps the turn-around, and those hops still add
        their own footstep peaks. Erratic handling has no peaks to add.
        """
        stop_s = float(self.config.pdr_gait_stop_s)
        window_s = float(self.config.pdr_gait_window_s)
        self._recent.append((self.t, motion))
        while self._recent and self.t - self._recent[0][0] > window_s + stop_s + 0.5:
            self._recent.popleft()
        if gait_ok:
            if self._rhythm_began is None:
                self._rhythm_began = self.t
            self._gait_fail_t = None
            self._gait_streak += 1
        elif self.walking or self._rhythm_began is not None:
            if self._gait_fail_t is None:
                self._gait_fail_t = self.t
            if self.t - self._gait_fail_t >= stop_s:
                self._close_gate()
                self._recent.clear()
                return
        else:
            return
        opened = False
        if not self.walking and self._gait_streak >= int(self.config.pdr_gait_start_windows):
            self.walking = True
            opened = True
            began = self._rhythm_began if self._rhythm_began is not None else self.t
            self.gate_events.append(("open", round(began, 3)))
        if not self.walking:
            return
        if opened:
            self._credit_confirming()
        elif not motion.get("erratic"):
            self._commit(motion)
            self._credited_through = self.t

    def mark(self, label: str) -> dict:
        point = {
            "label": label,
            "t": self.t,
            "x": float(self.pos[0]),
            "y": float(self.pos[1]),
            "steps": self.steps,
            "path_m": self.path_m,
            "mode": self.mode,
        }
        self.marks.append(point)
        return point

    def correct_toward(self, target_xy: tuple[float, float], reason: str) -> dict | None:
        """Pull position toward a visual match. Reject jumps over pdr_correction_max_m."""
        if not self.config.pdr_visual_correction:
            return None
        target = np.array(target_xy, dtype=np.float64)
        jump = float(np.linalg.norm(target - self.pos))
        if jump > float(self.config.pdr_correction_max_m):
            entry = {"t": self.t, "accepted": False, "jump_m": round(jump, 2), "reason": reason}
            self.corrections.append(entry)
            return entry
        gain = float(self.config.pdr_correction_gain)
        self.pos = self.pos + gain * (target - self.pos)
        entry = {"t": self.t, "accepted": True, "jump_m": round(jump, 2), "reason": reason}
        self.corrections.append(entry)
        return entry

    def live(self) -> dict:
        """Phone poll: steps, path length, gait gate, and marks. No path polyline."""
        return {
            "x": round(float(self.pos[0]), 3),
            "y": round(float(self.pos[1]), 3),
            "yaw": round(self.yaw, 3),
            "mode": self.mode,
            "confidence": round(self.confidence, 3),
            "steps": round(self.steps, 2),
            "path_m": round(self.path_m, 2),
            "step_length": float(self.config.pdr_step_length),
            "walking": bool(self.walking),
            "marks": list(self.marks[-20:]),
        }

    def public(self) -> dict:
        path = self.trace[-800:]
        return {
            "x": round(float(self.pos[0]), 3),
            "y": round(float(self.pos[1]), 3),
            "yaw": round(self.yaw, 3),
            "mode": self.mode,
            "confidence": round(self.confidence, 3),
            "steps": round(self.steps, 2),
            "path_m": round(self.path_m, 2),
            "step_length": float(self.config.pdr_step_length),
            "walking": self.walking,
            "acf_peak": round(self.acf_peak, 3),
            "band_ratio": round(self.band_ratio, 3),
            "path": [[round(p.x, 3), round(p.y, 3), p.mode] for p in path],
            "marks": self.marks[-20:],
            "corrections": self.corrections[-10:],
        }
