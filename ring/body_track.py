"""Body position from a phone in a pocket, fed by Sensor Logger's HTTP Push.

The JSON is the layout in Sensor Logger's PUSHING.md, UNITS.md, and
COORDINATES.md. A message is an object with messageId, sessionId, deviceId,
and payload. Each payload entry has name, time (UTC epoch nanoseconds), and
values. This reader uses only those fields.

Orientation values are yaw, pitch, and roll in radians, and the quaternion
qw, qx, qy, qz for that same attitude (intrinsic Z, then X, then Y). Pedometer
values are a cumulative steps count. Compass values are magneticBearing in
degrees. Accelerometer values are x, y, z. On iOS, with Standardise Units &
Frames left off, accelerometer is linear acceleration in m/s² with the
inertial sign, and orientation uses the x-north, z-vertical frame: yaw is 0
when device +x points north and yaw increases counterclockwise.

Heading comes from the orientation quaternion, projected onto the horizontal
plane, so a phone standing upright in a pocket still has a heading. The
gyroscope is used only when orientation has been missing for half a second.
The compass then corrects that fallback slowly. Each sample keeps its own
time field. Each step uses the heading at that step's own timestamp. A gap
longer than one second in the sample times breaks the path.

A step always counts. It moves the dot only when two gates are open at that
step's own sample time. The walk gate wants a vertical-acceleration rhythm in
the 1.4–2.5 Hz band, sustained for at least a second. A pocket repeats every
two steps, so that same cadence is also scored at twice the period. The turn
gate freezes the dot when yaw is above HOLD_YAW_DPS or the phone heading has
changed by more than 45 degrees in the last two seconds. A faster spin still
drops the step, as before. A step that is held does not move x, y, or the
path, and it does not teach the pocket offset.
"""

from __future__ import annotations

import json
import math
import threading
import uuid
from collections import deque
from pathlib import Path

FACE_DEG = 25.0
ARRIVE_M = 1.0
OFFSET_STEPS = 6
STEP_GAP_S = 0.32
STEP_HIGH = 2.2
STEP_LOW = 1.2
PEDOMETER_FRESH_S = 3.0
COMPASS_WEIGHT = 0.02
GAP_S = 1.0
# A step while the phone is spinning is the turn itself, not forward walking.
TURN_DPS = 50.0
TURN_WINDOW_S = 0.4
# Slower than a dropped spin. The step still counts, but the dot stays put.
HOLD_YAW_DPS = 40.0
HOLD_HEADING_DEG = 45.0
HOLD_HEADING_S = 2.0
# Walk gate: vertical rhythm in the step band, sustained for at least a second.
GAIT_HZ_LO = 1.4
GAIT_HZ_HI = 2.5
GAIT_WINDOW_S = 2.0
GAIT_MIN_S = 1.0
GAIT_MIN_STD = 0.40
GAIT_ACF_MIN = 0.20
# Slower than this cannot show a 2.5 Hz rhythm, so the gate is not scored.
GAIT_MAX_DT = 0.05
VERT_KEEP_S = 6.0
YAW_KEEP_S = 6.0
# Putting the phone into a pocket or taking it out rotates the whole phone, not just yaw.
HANDLE_DPS = 90.0
HANDLE_TILT_DEG = 30.0
HANDLE_WINDOW_S = 0.8
_ORDER = {
    "orientation": 0,
    "gyroscope": 1,
    "compass": 2,
    "gravity": 3,
    "accelerometer": 4,
    "pedometer": 5,
}

PUSH_PATH = "/api/sensor-logger"
BODY_PAGE = "/body"
BODY_SCRIPT = "/static/body.js"
BODY_SAVE = "/api/body/save"
BODY_LIVE = "/api/body/live"
BODY_FIND = "/api/body/find"
BODY_FIND_STOP = "/api/body/find/stop"
APP_PAGE = "/app"
APP_STYLE = "/static/phone.css"
APP_SCRIPT = "/static/phone.js"
APP_MANIFEST = "/manifest.webmanifest"
APP_ICON_192 = "/icons/icon-192.png"
APP_ICON_512 = "/icons/icon-512.png"
APP_TOUCH_ICON = "/apple-touch-icon.png"
HEALTH = "/api/health"


def phone_push_open(path: str, method: str) -> bool:
    """Routes the phone may use without the dashboard token."""
    if path == PUSH_PATH and method == "POST":
        return True
    if path in (
        BODY_PAGE,
        BODY_SCRIPT,
        APP_PAGE,
        APP_STYLE,
        APP_SCRIPT,
        APP_MANIFEST,
        APP_ICON_192,
        APP_ICON_512,
        APP_TOUCH_ICON,
    ) and method in ("GET", "HEAD"):
        return True
    if path in (BODY_LIVE, HEALTH) and method == "GET":
        return True
    if path in (BODY_SAVE, BODY_FIND, BODY_FIND_STOP) and method == "POST":
        return True
    return False


def wrap_deg(deg: float) -> float:
    """Shortest signed angle, in (-180, 180]."""
    return (float(deg) + 180.0) % 360.0 - 180.0


def norm_deg(deg: float) -> float:
    return float(deg) % 360.0


def signed_diff(target: float, heading: float) -> float:
    """target minus heading, wrapped. Positive means target is to the right."""
    return wrap_deg(float(target) - float(heading))


def circular_mean(degrees: list[float]) -> float:
    if not degrees:
        return 0.0
    east = sum(math.sin(math.radians(deg)) for deg in degrees)
    north = sum(math.cos(math.radians(deg)) for deg in degrees)
    if east * east + north * north < 1e-12:
        return norm_deg(degrees[-1])
    return norm_deg(math.degrees(math.atan2(east, north)))


def _rotation(yaw: float, pitch: float, roll: float) -> tuple[tuple[float, float, float], ...]:
    """Device vector to the x-north, y-west, z-up frame. Intrinsic yaw, pitch, roll."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    return (
        (cy * cr + sy * sp * sr, -sy * cp, cy * sr - sy * sp * cr),
        (sy * cr - cy * sp * sr, cy * cp, sy * sr + cy * sp * cr),
        (-cp * sr, sp, cp * cr),
    )


def _matrix_from_quaternion(
    qw: float, qx: float, qy: float, qz: float
) -> tuple[tuple[float, float, float], ...] | None:
    """Same frame as _rotation. The quaternion is qz(yaw) then qx(pitch) then qy(roll)."""
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if not math.isfinite(norm) or norm < 1e-6:
        return None
    qw, qx, qy, qz = qw / norm, qx / norm, qy / norm, qz / norm
    return (
        (1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)),
        (2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)),
        (2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)),
    )


def _apply(matrix, vx: float, vy: float, vz: float) -> tuple[float, float, float]:
    north = matrix[0][0] * vx + matrix[0][1] * vy + matrix[0][2] * vz
    west = matrix[1][0] * vx + matrix[1][1] * vy + matrix[1][2] * vz
    up = matrix[2][0] * vx + matrix[2][1] * vy + matrix[2][2] * vz
    return -west, north, up


def _horizontal(east: float, north: float) -> tuple[float | None, float]:
    strength = math.hypot(east, north)
    if strength < 1e-3:
        return None, 0.0
    return norm_deg(math.degrees(math.atan2(east, north))), strength


def _acf_peak(values: list[float], dt: float, lag_lo_s: float, lag_hi_s: float) -> float:
    """Largest autocorrelation between two lags, normalized by the signal energy."""
    count = len(values)
    if count < 16 or dt <= 0:
        return 0.0
    mean = sum(values) / count
    centered = [value - mean for value in values]
    energy = sum(value * value for value in centered)
    if energy < 1e-8:
        return 0.0
    lag_lo = max(1, int(round(lag_lo_s / dt)))
    lag_hi = min(count - 2, int(round(lag_hi_s / dt)))
    if lag_hi <= lag_lo:
        return 0.0
    peak = 0.0
    for lag in range(lag_lo, lag_hi + 1):
        corr = sum(centered[index] * centered[index + lag] for index in range(count - lag))
        if corr > peak:
            peak = corr
    return peak / energy


AWAY_S = 1.8
ARRIVE_S = 0.32
CLOSE_S = 0.40


def guide_interval(distance_m: float, error_deg: float) -> dict:
    """Pulse faster as the phone gets closer. A wrong heading only slows the pace a little.

    Just outside 1 m is about 0.4 s. At 4 m it is about 1.1 s. At 7 m and beyond it is 1.8 s.
    Under 1 m the ring pulses in every direction. That is the arrival burst.
    error_deg is wrapped, so 350° and -10° are the same miss.
    """
    error = abs(wrap_deg(error_deg))
    facing = error <= FACE_DEG
    if distance_m < ARRIVE_M:
        return {"mode": "arrival", "interval_s": ARRIVE_S, "facing": True, "arrived": True}
    span = min(1.0, max(0.0, (float(distance_m) - ARRIVE_M) / 6.0))
    interval = CLOSE_S + (AWAY_S - CLOSE_S) * span
    if not facing:
        interval = min(AWAY_S, interval + 0.12)
    interval = min(AWAY_S, max(ARRIVE_S + 0.04, interval))
    return {
        "mode": "facing" if facing else "away",
        "interval_s": round(interval, 3),
        "facing": facing,
        "arrived": False,
    }


class BodyTracker:
    def __init__(self, step_length: float = 0.75, log_dir: Path | None = None) -> None:
        self.step_length = float(step_length)
        self.log_dir = None if log_dir is None else Path(log_dir)
        self._log_lock = threading.Lock()
        self.x = 0.0
        self.y = 0.0
        self.steps = 0
        self.path_length = 0.0
        self.path: list[list[float]] = [[0.0, 0.0, 0]]
        self.heading: float | None = None
        self.phone_heading: float | None = None
        self.pocket_offset = 0.0
        self.offset_samples: list[float] = []
        self.offset_locked = False
        # Coordinate frame for this process. A quiet phone does not change it.
        self.frame_id = uuid.uuid4().hex
        self.session_id = ""
        self.device_id = ""
        self.message_count = 0
        self.sensors: set[str] = set()
        self.packets: deque[float] = deque()
        self.last_packet: float | None = None
        self.guide: dict | None = None
        self.loop: dict | None = None
        self._arm_steps: int | None = None
        self._matrix = None
        self._axis: str | None = None
        self._axis_shift = 0.0
        self._horiz: list[tuple[float, float]] = []
        self._pedo_last: int | None = None
        self._pedo_at: float | None = None
        self._step_armed = True
        self._last_step_t: float | None = None
        self._log_name = ""
        self._heading_track: list[tuple[float, float]] = []
        self._gyro_t: float | None = None
        self._gyro_at: float | None = None
        self._yaw_abs: deque[tuple[float, float, float]] = deque()
        self._vert: deque[tuple[float, float]] = deque()
        self._advancing = True
        self._hold_reason = ""
        self._up_track: deque[tuple[float, tuple[float, float, float]]] = deque()
        self._orient_at: float | None = None
        self._compass_t: float | None = None
        self._heading_source: str | None = None
        self._clock: dict[str, float] = {}
        self._gap_prev: dict[str, float] = {}
        self._gaps: dict[str, deque[float]] = {}
        self._last_sample_t: float | None = None
        self._gap_pending = False
        self._up = (0.0, 0.0, 1.0)
        self._rate_times: dict[str, deque[float]] = {}
        self.stalls: list[dict] = []

    def ingest(self, message: dict, now: float) -> dict:
        """Accept one HTTP Push message. Readings inside it are ordered by time."""
        wall_anchor = self.last_packet
        self._note_packet(now)
        if not isinstance(message, dict):
            return self.public(now)
        session = message.get("sessionId")
        if isinstance(session, str) and session and session != self.session_id:
            self.session_id = session
            device = message.get("deviceId")
            self.device_id = device if isinstance(device, str) else ""
            self._log({"event": "session", "sessionId": self.session_id, "deviceId": self.device_id})
        payload = message.get("payload")
        readings = []
        if isinstance(payload, list):
            for reading in payload:
                parsed = _reading(reading)
                if parsed is not None:
                    readings.append(parsed)
        readings = self._stamp(readings)
        readings.sort(key=lambda item: (item[1], _ORDER.get(item[0], 9)))
        self._note_wall_gap(now, wall_anchor, readings)
        for name, time_s, values in readings:
            self.sensors.add(name)
            when = time_s
            self._count_sample(name, when)
            self._see_time(when)
            self._log({"event": "sample", "n": name, "t": round(when, 6), "v": _plain(values)})
            if name == "orientation":
                self._orientation(when, values)
            elif name == "gyroscope":
                self._gyro(when, values)
            elif name == "compass":
                self._compass(when, values)
            elif name == "accelerometer":
                self._accelerometer(when, values)
            elif name == "pedometer":
                self._pedometer(when, values)
        self._refresh_hold(self._last_sample_t)
        self._log(
            {
                "event": "push",
                "messageId": message.get("messageId"),
                "sessionId": self.session_id,
                "steps": self.steps,
                "heading": None if self.heading is None else round(self.heading, 2),
                "x": round(self.x, 3),
                "y": round(self.y, 3),
            }
        )
        return self.public(now)

    def guide_to(self, target_x: float, target_y: float, name: str) -> dict | None:
        if self.heading is None:
            return None
        east = float(target_x) - self.x
        north = float(target_y) - self.y
        distance = math.hypot(east, north)
        bearing = norm_deg(math.degrees(math.atan2(east, north))) if distance >= 1e-3 else self.heading
        error = signed_diff(bearing, self.heading)
        pace = guide_interval(distance, error)
        return {
            "name": name,
            "target_x": round(float(target_x), 3),
            "target_y": round(float(target_y), 3),
            "distance_m": round(distance, 2),
            "bearing": round(bearing, 1),
            "error_deg": round(error, 1),
            **pace,
        }

    def recalibrate_offset(self) -> dict:
        """Forget the pocket offset. The next steps set a new one, then it freezes again."""
        self.offset_locked = False
        self.offset_samples.clear()
        self.pocket_offset = 0.0
        self._log({"event": "recalibrate"})
        return {"offset_state": "unset"}

    def arm_calibration(self) -> dict:
        self._arm_steps = self.steps
        return {"armed_steps": self.steps}

    def calibrate(self, metres: float) -> float:
        """Step length from a straight walk of a known distance, in metres."""
        if self._arm_steps is None:
            raise ValueError("Start the calibration walk first.")
        walked = self.steps - self._arm_steps
        if walked < 1:
            raise ValueError("Walk at least one step after starting the calibration.")
        if not math.isfinite(metres) or metres <= 0 or metres > 500:
            raise ValueError("Enter the distance you walked, in metres.")
        self.step_length = metres / walked
        self._arm_steps = None
        self._log({"event": "calibrate", "metres": metres, "steps": walked, "step_length": self.step_length})
        return self.step_length

    def loop_start(self) -> dict:
        self.loop = {
            "open": True,
            "x": self.x,
            "y": self.y,
            "steps": self.steps,
            "path_m": self.path_length,
        }
        self._log({"event": "loop_start", "x": round(self.x, 3), "y": round(self.y, 3), "steps": self.steps})
        return {"started": True}

    def loop_end(self) -> dict:
        if self.loop is None or not self.loop.get("open"):
            raise ValueError("Mark the start of the loop first, then walk back to that spot.")
        start = self.loop
        report = {
            "open": False,
            "steps": self.steps - int(start["steps"]),
            "path_m": round(self.path_length - float(start["path_m"]), 2),
            "closure_m": round(math.hypot(self.x - float(start["x"]), self.y - float(start["y"])), 2),
        }
        self.loop = report
        self._log({"event": "loop_end", **report})
        return report

    def has_fix(self) -> bool:
        return self.phone_heading is not None or self.steps > 0

    def public(self, now: float | None = None) -> dict:
        age = None
        if self.last_packet is not None and now is not None:
            age = round(max(0.0, now - self.last_packet), 2)
        rate = 0.0
        if now is not None and self.packets:
            rate = len(self.packets) / 2.0
        return {
            "packet_rate": round(rate, 2),
            "packet_age_s": age,
            "sample_rates": self._sample_rates(),
            "sample_gap_ms": self._median_gaps_ms(),
            "stalls": self.stalls[-6:],
            "heading": None if self.heading is None else round(self.heading, 1),
            "phone_heading": None if self.phone_heading is None else round(self.phone_heading, 1),
            "heading_source": self._heading_source,
            "steps": self.steps,
            "advancing": self._advancing,
            "hold_reason": self._hold_reason,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "path_m": round(self.path_length, 2),
            "path": self.path[-1500:],
            "step_length": round(self.step_length, 3),
            "pocket_offset": None if not self.offset_samples and not self.offset_locked else round(
                self.pocket_offset if self.offset_locked else circular_mean(self.offset_samples), 1
            ),
            "offset_steps": len(self.offset_samples),
            "offset_needed": OFFSET_STEPS,
            "offset_state": "frozen" if self.offset_locked else ("learning" if self.offset_samples else "unset"),
            "calibrated": self.offset_locked,
            "sensors": sorted(self.sensors),
            "session_id": self.session_id,
            "frame_id": self.frame_id,
            "guide": self.guide,
            "loop": self.loop,
            "calibration_armed": self._arm_steps is not None,
            "steps_since_arm": None if self._arm_steps is None else self.steps - self._arm_steps,
        }

    def _note_packet(self, now: float) -> None:
        self.message_count += 1
        self.last_packet = now
        self.packets.append(now)
        while self.packets and now - self.packets[0] > 2.0:
            self.packets.popleft()

    def _note_wall_gap(self, now: float, previous: float | None, readings: list[tuple[str, float, dict]]) -> None:
        if previous is None:
            return
        wall = now - previous
        if wall <= GAP_S:
            return
        times = [item[1] for item in readings if item[1] > 0]
        sensor = None if not times or self._last_sample_t is None else min(times) - self._last_sample_t
        if sensor is not None and sensor > GAP_S:
            return
        # A 1 s post with samples 10 ms apart is normal batching, not a stall.
        if sensor is not None and sensor <= 0.05:
            return
        if sensor is not None:
            reason = (
                f"Posts were {wall:.1f} s apart. The samples inside them are {sensor * 1000:.0f} ms apart."
            )
        else:
            reason = (
                f"No post reached the laptop for {wall:.1f} s. "
                "The phone left the Wi-Fi, or the recording stopped."
            )
            self._gap_pending = True
        self._add_stall(wall, sensor, reason)

    def _see_time(self, time_s: float) -> None:
        if time_s <= 0:
            return
        if self._last_sample_t is not None and time_s - self._last_sample_t > GAP_S:
            gap = time_s - self._last_sample_t
            self._gap_pending = True
            self._add_stall(
                None,
                gap,
                f"The phone stopped sampling for {gap:.1f} s. The recording was paused, "
                "the app left the foreground, or the phone locked.",
            )
        if self._last_sample_t is None or time_s > self._last_sample_t:
            self._last_sample_t = time_s

    def _add_stall(self, wall: float | None, sensor: float | None, reason: str) -> None:
        self.stalls.append(
            {
                "wall_s": None if wall is None else round(wall, 2),
                "sensor_s": None if sensor is None else round(sensor, 2),
                "reason": reason,
            }
        )
        if len(self.stalls) > 20:
            self.stalls = self.stalls[-20:]

    def _count_sample(self, name: str, time_s: float) -> None:
        if time_s <= 0:
            return
        bucket = self._rate_times.setdefault(name, deque())
        bucket.append(time_s)
        while bucket and time_s - bucket[0] > 2.0:
            bucket.popleft()

    def _stamp(self, readings: list[tuple[str, float | None, dict]]) -> list[tuple[str, float, dict]]:
        """Keep each sample's own time. Space a sample only when it has none."""
        stamped: list[tuple[str, float, dict]] = []
        for name, time_s, values in readings:
            if time_s is None:
                time_s = self._spaced(name)
            else:
                self._clock[name] = time_s
                self._note_gap(name, time_s)
            stamped.append((name, time_s, values))
        return stamped

    def _spaced(self, name: str) -> float:
        rate = self._rate_hint(name)
        previous = self._clock.get(name)
        when = (0.0 if previous is None else previous) + 1.0 / rate
        self._clock[name] = when
        return when

    def _rate_hint(self, name: str) -> float:
        gaps = self._gaps.get(name)
        if gaps:
            ordered = sorted(gaps)
            median = ordered[len(ordered) // 2]
            if median > 1e-4:
                return 1.0 / median
        return 100.0

    def _note_gap(self, name: str, time_s: float) -> None:
        previous = self._gap_prev.get(name)
        self._gap_prev[name] = time_s
        if previous is None:
            return
        gap = time_s - previous
        if gap <= 0 or gap > 0.5:
            return
        self._gaps.setdefault(name, deque(maxlen=400)).append(gap)

    def _median_gaps_ms(self) -> dict[str, float]:
        gaps_ms = {}
        for name, gaps in self._gaps.items():
            if not gaps:
                continue
            ordered = sorted(gaps)
            gaps_ms[name] = round(ordered[len(ordered) // 2] * 1000.0, 1)
        return gaps_ms

    def _attitude_fresh(self, time_s: float) -> bool:
        return self._orient_at is not None and 0.0 <= time_s - self._orient_at <= 0.5

    def _sample_rates(self) -> dict[str, float]:
        rates = {}
        for name, bucket in self._rate_times.items():
            if len(bucket) < 2:
                continue
            latest = max(bucket)
            fresh = [stamp for stamp in bucket if latest - stamp <= 2.0]
            if len(fresh) < 2:
                continue
            span = max(fresh) - min(fresh)
            if span <= 0:
                continue
            rates[name] = round((len(fresh) - 1) / span, 1)
        return rates

    def heading_at(self, time_s: float) -> float | None:
        """Phone heading at one timestamp, interpolated on the short arc."""
        track = self._heading_track
        if not track:
            return self.phone_heading
        if time_s <= track[0][0]:
            return track[0][1]
        if time_s >= track[-1][0]:
            return track[-1][1]
        lo, hi = 0, len(track) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if track[mid][0] <= time_s:
                lo = mid
            else:
                hi = mid
        t0, h0 = track[lo]
        t1, h1 = track[hi]
        if t1 <= t0:
            return h1
        return norm_deg(h0 + signed_diff(h1, h0) * ((time_s - t0) / (t1 - t0)))

    def _remember(self, time_s: float) -> None:
        if self.phone_heading is None:
            return
        if self._heading_track and time_s < self._heading_track[-1][0]:
            return
        if self._heading_track and time_s == self._heading_track[-1][0]:
            self._heading_track[-1] = (time_s, self.phone_heading)
        else:
            self._heading_track.append((time_s, self.phone_heading))
        if len(self._heading_track) > 8000:
            self._heading_track = self._heading_track[-6000:]

    def _orientation(self, time_s: float, values: dict) -> None:
        quat = self._quaternion(values)
        matrix = None if quat is None else _matrix_from_quaternion(*quat)
        if matrix is None:
            try:
                yaw = float(values["yaw"])
                pitch = float(values["pitch"])
                roll = float(values["roll"])
            except (KeyError, TypeError, ValueError):
                return
            if not all(math.isfinite(number) for number in (yaw, pitch, roll)):
                return
            matrix = _rotation(yaw, pitch, roll)
        self._matrix = matrix
        self._up = (matrix[2][0], matrix[2][1], matrix[2][2])
        self._up_track.append((time_s, self._up))
        while self._up_track and time_s - self._up_track[0][0] > HANDLE_WINDOW_S:
            self._up_track.popleft()
        self._orient_at = time_s
        fused = self._heading_from(matrix)
        if fused is not None:
            self.phone_heading = fused
            self._heading_source = "orientation"
        self._publish_heading()
        self._remember(time_s)

    def _quaternion(self, values: dict) -> tuple[float, float, float, float] | None:
        try:
            quat = (float(values["qw"]), float(values["qx"]), float(values["qy"]), float(values["qz"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(number) for number in quat):
            return None
        return quat

    def _gyro(self, time_s: float, values: dict) -> None:
        try:
            x = float(values["x"])
            y = float(values["y"])
            z = float(values["z"])
        except (KeyError, TypeError, ValueError):
            return
        if not all(math.isfinite(number) for number in (x, y, z)):
            return
        previous = self._gyro_t
        self._gyro_t = time_s
        self._gyro_at = time_s
        self._note_turn(time_s, x, y, z)
        if self._attitude_fresh(time_s):
            return
        if self.phone_heading is None or previous is None:
            self._remember(time_s)
            return
        dt = time_s - previous
        if dt <= 0 or dt > GAP_S:
            return
        # Positive rotation about up is counterclockwise. Compass heading falls.
        rate = x * self._up[0] + y * self._up[1] + z * self._up[2]
        self.phone_heading = norm_deg(self.phone_heading - math.degrees(rate * dt))
        self._heading_source = "gyro"
        self._publish_heading()
        self._remember(time_s)

    def _compass(self, time_s: float, values: dict) -> None:
        try:
            bearing = float(values["magneticBearing"])
        except (KeyError, TypeError, ValueError):
            return
        if not math.isfinite(bearing) or bearing < 0:
            return
        bearing = norm_deg(bearing)
        if self._attitude_fresh(time_s):
            return
        if self.phone_heading is None:
            self.phone_heading = bearing
            self._heading_source = "compass"
        else:
            elapsed = 0.1 if self._compass_t is None else max(0.0, time_s - self._compass_t)
            weight = COMPASS_WEIGHT * min(1.0, elapsed / 0.1)
            self.phone_heading = norm_deg(
                self.phone_heading + weight * signed_diff(bearing, self.phone_heading)
            )
        self._compass_t = time_s
        self._publish_heading()
        self._remember(time_s)

    def _heading_from(self, matrix) -> float | None:
        axes = {
            "+y": (0.0, 1.0, 0.0),
            "+x": (1.0, 0.0, 0.0),
            "-z": (0.0, 0.0, -1.0),
        }
        measured = {name: _horizontal(*_apply(matrix, *vector)[:2]) for name, vector in axes.items()}
        if self._axis is None:
            if measured["+y"][1] >= 0.5:
                self._axis = "+y"
            else:
                self._axis = max(measured, key=lambda name: measured[name][1])
        current = measured[self._axis]
        if current[1] < 0.25:
            stronger = max(measured, key=lambda name: measured[name][1])
            other_deg, other_h = measured[stronger]
            if stronger != self._axis and other_h > 0.55 and other_deg is not None and self.phone_heading is not None:
                self._axis_shift = signed_diff(self.phone_heading, other_deg)
                self._axis = stronger
                current = (other_deg, other_h)
            else:
                return self.phone_heading
        if current[0] is None:
            return self.phone_heading
        return norm_deg(current[0] + self._axis_shift)

    def _publish_heading(self) -> None:
        if self.phone_heading is None:
            return
        offset = self.pocket_offset if self.offset_locked else (
            circular_mean(self.offset_samples) if self.offset_samples else 0.0
        )
        self.heading = norm_deg(self.phone_heading + offset)

    def _set_phone_heading(self, heading: float | None) -> None:
        if heading is None:
            return
        self.phone_heading = heading
        offset = self.pocket_offset if self.offset_locked else (
            circular_mean(self.offset_samples) if self.offset_samples else 0.0
        )
        self.heading = norm_deg(heading + offset)

    def _accelerometer(self, time_s: float, values: dict) -> None:
        try:
            x = float(values["x"])
            y = float(values["y"])
            z = float(values["z"])
        except (KeyError, TypeError, ValueError):
            return
        if not all(math.isfinite(number) for number in (x, y, z)):
            return
        # iOS reports the inertial acceleration. The step's direction of motion
        # is the opposite vector. Magnitude is the same either way.
        if self._matrix is not None:
            east, north, up = _apply(self._matrix, -x, -y, -z)
            self._horiz.append((east, north))
            if len(self._horiz) > 400:
                del self._horiz[:200]
            self._vert.append((time_s, up))
            while self._vert and time_s - self._vert[0][0] > VERT_KEEP_S:
                self._vert.popleft()
        if self._pedo_at is not None and time_s - self._pedo_at <= PEDOMETER_FRESH_S:
            return
        magnitude = math.sqrt(x * x + y * y + z * z)
        if not self._step_armed and magnitude < STEP_LOW:
            self._step_armed = True
        if (
            self._step_armed
            and magnitude > STEP_HIGH
            and (self._last_step_t is None or time_s - self._last_step_t >= STEP_GAP_S)
        ):
            self._step_armed = False
            self._take_step(time_s)

    def _pedometer(self, time_s: float, values: dict) -> None:
        try:
            steps = int(values["steps"])
        except (KeyError, TypeError, ValueError):
            return
        previous = self._pedo_at
        self._pedo_at = time_s
        if self._pedo_last is None:
            self._pedo_last = steps
            return
        delta = steps - self._pedo_last
        if delta < 0:
            self._pedo_last = steps
            return
        self._pedo_last = steps
        # A real walk is slower than about three steps a second. A single
        # batch that jumps further than that is a counter reset, not a sprint.
        span = 0.4 if previous is None else max(0.4, time_s - previous)
        limit = max(1, int(span / 0.22))
        accepted = min(delta, limit)
        start = time_s if previous is None else previous
        for index in range(accepted):
            when = time_s if accepted == 1 else start + (index + 1) * (time_s - start) / accepted
            self._take_step(when)

    def _take_step(self, time_s: float) -> None:
        phone = self.heading_at(time_s)
        if phone is None:
            self._horiz.clear()
            return
        if self._spinning(time_s) or self._handling(time_s):
            # A turn, or the phone going into or out of the pocket, is not a step.
            self._horiz.clear()
            self._last_step_t = time_s
            return
        advance, reason = self._advance_decision(time_s)
        self._advancing = advance
        self._hold_reason = reason
        if not advance:
            # Counted, but it does not move the dot or teach the pocket offset.
            self._horiz.clear()
            self._last_step_t = time_s
            self.steps += 1
            self._log(
                {
                    "event": "step",
                    "steps": self.steps,
                    "heading": round(phone, 2),
                    "x": round(self.x, 3),
                    "y": round(self.y, 3),
                    "held": reason,
                }
            )
            return
        travel = self._travel_heading()
        self._horiz.clear()
        # The offset is learned once, then frozen. A pause, a new phone
        # recording, or the next walking stretch must not change it.
        if travel is not None and not self.offset_locked:
            self.offset_samples.append(signed_diff(travel, phone))
            if len(self.offset_samples) >= OFFSET_STEPS:
                self.pocket_offset = circular_mean(self.offset_samples[:OFFSET_STEPS])
                self.offset_locked = True
        offset = self.pocket_offset if self.offset_locked else (
            circular_mean(self.offset_samples) if self.offset_samples else 0.0
        )
        step_heading = norm_deg(phone + offset)
        self._publish_heading()
        self._last_step_t = time_s
        broken = 1 if self._gap_pending else 0
        self._gap_pending = False
        radians = math.radians(step_heading)
        self.x += self.step_length * math.sin(radians)
        self.y += self.step_length * math.cos(radians)
        self.steps += 1
        self.path_length += self.step_length
        self.path.append([round(self.x, 3), round(self.y, 3), broken])
        if len(self.path) > 4000:
            self.path = self.path[-3000:]
        self._log(
            {
                "event": "step",
                "steps": self.steps,
                "heading": round(step_heading, 2),
                "x": round(self.x, 3),
                "y": round(self.y, 3),
                "pocket_offset": None if not self.offset_locked else round(self.pocket_offset, 2),
            }
        )

    def _note_turn(self, time_s: float, x: float, y: float, z: float) -> None:
        yaw = abs(math.degrees(x * self._up[0] + y * self._up[1] + z * self._up[2]))
        total = abs(math.degrees(math.sqrt(x * x + y * y + z * z)))
        self._yaw_abs.append((time_s, yaw, total))
        while self._yaw_abs and time_s - self._yaw_abs[0][0] > YAW_KEEP_S:
            self._yaw_abs.popleft()

    def _refresh_hold(self, time_s: float | None) -> None:
        if time_s is None or time_s <= 0:
            return
        advancing, reason = self._advance_decision(time_s)
        self._advancing = advancing
        self._hold_reason = reason

    def _advance_decision(self, time_s: float) -> tuple[bool, str]:
        """Whether a step at this sample time may move the dot."""
        if self._turning(time_s):
            return False, "turning"
        rhythm = self._walking_rhythm(time_s)
        if rhythm is False:
            return False, "no walking rhythm"
        return True, ""

    def _turning(self, time_s: float) -> bool:
        yaw = self._yaw_rate(time_s)
        if yaw is not None and yaw > HOLD_YAW_DPS:
            return True
        return self._heading_swing(time_s) > HOLD_HEADING_DEG

    def _yaw_rate(self, time_s: float) -> float | None:
        fresh = [
            yaw
            for stamp, yaw, _total in self._yaw_abs
            if 0.0 <= time_s - stamp <= TURN_WINDOW_S
        ]
        if len(fresh) < 3:
            return None
        return sum(fresh) / len(fresh)

    def _heading_swing(self, time_s: float) -> float:
        """How far the phone heading moved during the last two seconds, in degrees."""
        now_h = self.heading_at(time_s)
        then_h = self.heading_at(time_s - HOLD_HEADING_S)
        swing = 0.0 if now_h is None or then_h is None else abs(signed_diff(now_h, then_h))
        samples = [heading for stamp, heading in self._heading_track if time_s - HOLD_HEADING_S <= stamp <= time_s]
        if len(samples) < 2:
            return swing
        unwrapped = [0.0]
        current = samples[0]
        for heading in samples[1:]:
            delta = signed_diff(heading, current)
            unwrapped.append(unwrapped[-1] + delta)
            current = heading
        return max(swing, max(unwrapped) - min(unwrapped))

    def _walking_rhythm(self, time_s: float) -> bool | None:
        """True when vertical acceleration has a sustained step rhythm.

        None means the samples cannot show a 1.4–2.5 Hz band, so the gate
        stays open. False means the band was scored and the rhythm is absent.
        A pocket cycle is two steps, so the autocorrelation is taken at the
        step period and at twice that period.
        """
        rows = [row for row in self._vert if time_s - GAIT_WINDOW_S <= row[0] <= time_s]
        if len(rows) < 8:
            return None
        gaps = [rows[index][0] - rows[index - 1][0] for index in range(1, len(rows))]
        gaps = [gap for gap in gaps if gap > 1e-4]
        if not gaps:
            return None
        gaps.sort()
        dt = gaps[len(gaps) // 2]
        if dt > GAIT_MAX_DT:
            return None
        span = rows[-1][0] - rows[0][0]
        if span < GAIT_MIN_S or len(rows) < 16:
            return False
        values = [row[1] for row in rows]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        if math.sqrt(variance) < GAIT_MIN_STD:
            return False
        step_peak = _acf_peak(values, dt, 1.0 / GAIT_HZ_HI, 1.0 / GAIT_HZ_LO)
        stride_peak = _acf_peak(values, dt, 2.0 / GAIT_HZ_HI, 2.0 / GAIT_HZ_LO)
        return max(step_peak, stride_peak) >= GAIT_ACF_MIN

    def _spinning(self, time_s: float) -> bool:
        fresh = [
            (yaw, total)
            for stamp, yaw, total in self._yaw_abs
            if 0.0 <= time_s - stamp <= TURN_WINDOW_S
        ]
        if len(fresh) < 5:
            return False
        yaw = sum(item[0] for item in fresh) / len(fresh)
        total = sum(item[1] for item in fresh) / len(fresh)
        return yaw > TURN_DPS or total > HANDLE_DPS

    def _handling(self, time_s: float) -> bool:
        """True when the phone has tilted like it is being pocketed or taken out."""
        worst = 0.0
        for stamp, up in self._up_track:
            if time_s - stamp > HANDLE_WINDOW_S or time_s < stamp:
                continue
            dot = up[0] * self._up[0] + up[1] * self._up[1] + up[2] * self._up[2]
            dot = max(-1.0, min(1.0, dot))
            worst = max(worst, math.degrees(math.acos(dot)))
        return worst > HANDLE_TILT_DEG

    def _travel_heading(self) -> float | None:
        if not self._horiz:
            return None
        east = sum(sample[0] for sample in self._horiz)
        north = sum(sample[1] for sample in self._horiz)
        heading, strength = _horizontal(east, north)
        if heading is None or strength < 0.8:
            return None
        return heading

    def _log(self, row: dict) -> None:
        if self.log_dir is None or not self.session_id:
            return
        safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in self.session_id)[:80]
        if not safe:
            return
        if safe != self._log_name:
            self._log_name = safe
        path = self.log_dir / f"{safe}.jsonl"
        line = json.dumps(row, separators=(",", ":")) + "\n"
        with self._log_lock:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)


def _plain(values: dict) -> dict:
    plain = {}
    for key, value in values.items():
        if isinstance(value, bool) or value is None:
            plain[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            plain[key] = round(float(value), 6) if isinstance(value, float) else value
        elif isinstance(value, str) and len(value) <= 80:
            plain[key] = value
    return plain


def _epoch_seconds(value: object) -> float | None:
    """Sensor Logger's time field is nanoseconds since the UTC epoch."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number / 1e9


def _reading(reading: object) -> tuple[str, float | None, dict] | None:
    if not isinstance(reading, dict):
        return None
    name = reading.get("name")
    values = reading.get("values")
    if not isinstance(name, str) or not isinstance(values, dict):
        return None
    return name.strip().lower(), _epoch_seconds(reading.get("time")), values
