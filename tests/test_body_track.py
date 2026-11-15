"""Phone body tracking from Sensor Logger's documented HTTP Push JSON."""

from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from ring.body_track import (
    BODY_PAGE,
    PUSH_PATH,
    BodyTracker,
    guide_interval,
    norm_deg,
    phone_push_open,
    signed_diff,
    wrap_deg,
)


def _quaternion(yaw: float, pitch: float, roll: float) -> tuple[float, float, float, float]:
    """qz(yaw) then qx(pitch) then qy(roll), the Sensor Logger orientation quaternion."""
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    return (
        cy * cp * cr - sy * sp * sr,
        cy * sp * cr - sy * cp * sr,
        cy * cp * sr + sy * sp * cr,
        cy * sp * sr + sy * cp * cr,
    )


def _orientation(second: float, yaw: float, pitch: float = 0.0, roll: float = 0.0) -> dict:
    qw, qx, qy, qz = _quaternion(yaw, pitch, roll)
    return {
        "name": "orientation",
        "time": int(second * 1e9),
        "values": {"yaw": yaw, "pitch": pitch, "roll": roll, "qw": qw, "qx": qx, "qy": qy, "qz": qz},
    }


def _accel(second: float, x: float, y: float, z: float) -> dict:
    return {"name": "accelerometer", "time": int(second * 1e9), "values": {"x": x, "y": y, "z": z}}


def _steps(second: float, steps: int) -> dict:
    return {"name": "pedometer", "time": int(second * 1e9), "values": {"steps": steps}}


def _message(*readings, session: str = "walk-1", message_id: int = 1) -> dict:
    return {
        "messageId": message_id,
        "sessionId": session,
        "deviceId": "iphone",
        "payload": list(readings),
    }


class AngleTests(unittest.TestCase):
    def test_wrap_is_the_short_way(self) -> None:
        self.assertAlmostEqual(signed_diff(10, 350), 20)
        self.assertAlmostEqual(signed_diff(350, 10), -20)
        self.assertAlmostEqual(signed_diff(143, 323), -180)
        self.assertAlmostEqual(abs(wrap_deg(540)), 180)
        self.assertAlmostEqual(norm_deg(-90), 270)

    def test_flat_phone_axes(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(_message(_orientation(0, 0)), now=0)
        # yaw 0: device +x is north, device +y is west. The top is the heading.
        self.assertAlmostEqual(tracker.phone_heading, 270, places=5)

    def test_tilt_does_not_reverse_heading(self) -> None:
        tracker = BodyTracker()
        yaw = math.radians(-53)  # device +y heading 323°
        previous = None
        for step in range(0, 15):
            pitch = step * (1.2 / 14)
            tracker.ingest(_message(_orientation(step, yaw, pitch)), now=step)
            heading = tracker.phone_heading
            self.assertIsNotNone(heading)
            if previous is not None:
                self.assertLess(abs(signed_diff(heading, previous)), 20)
            previous = heading
        self.assertLess(abs(signed_diff(tracker.phone_heading, 323)), 8)

    def test_turn_stays_continuous(self) -> None:
        tracker = BodyTracker()
        start = math.radians(-53)
        previous = None
        for step in range(0, 19):
            yaw = start + math.radians(step * 10)
            tracker.ingest(_message(_orientation(step, yaw)), now=step)
            heading = tracker.phone_heading
            if previous is not None:
                self.assertAlmostEqual(signed_diff(heading, previous), -10, places=5)
            previous = heading
        self.assertAlmostEqual(signed_diff(tracker.phone_heading, 323), -180, places=5)


class StepTests(unittest.TestCase):
    def test_pedometer_and_pocket_offset(self) -> None:
        tracker = BodyTracker(step_length=0.7)
        # Flat, yaw 0. Phone top points west (270). Motion is north, which is
        # device +x. iOS acceleration is the inertial sign, so the reading is -x.
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(1, 7):
            readings.append(_accel(index - 0.1, -3.0, 0.0, 0.0))
            readings.append(_steps(index, index))
        tracker.ingest(_message(*readings), now=1)
        self.assertTrue(tracker.offset_locked)
        self.assertAlmostEqual(tracker.pocket_offset, 90, places=4)
        self.assertEqual(tracker.steps, 6)
        self.assertAlmostEqual(tracker.heading, 0, places=4)
        self.assertAlmostEqual(tracker.y, 6 * 0.7, places=4)
        self.assertAlmostEqual(tracker.x, 0, places=4)
        self.assertEqual(tracker.public(1)["offset_state"], "frozen")

    def test_offset_freezes_until_recalibrate(self) -> None:
        tracker = BodyTracker(step_length=0.7)
        frame = tracker.frame_id
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(1, 9):
            readings.append(_accel(index - 0.1, -3.0, 0.0, 0.0))
            readings.append(_steps(index, index))
        tracker.ingest(_message(*readings), now=1)
        frozen = tracker.pocket_offset
        self.assertTrue(tracker.offset_locked)
        # A long pause, then steps whose travel points the other way.
        later = [_orientation(20, 0)]
        for index in range(9, 17):
            later.append(_accel(14 + index - 0.1, 3.0, 0.0, 0.0))
            later.append(_steps(14 + index, index))
        tracker.ingest(_message(*later, session="walk-2"), now=20)
        self.assertEqual(tracker.frame_id, frame)
        self.assertEqual(tracker.session_id, "walk-2")
        self.assertAlmostEqual(tracker.pocket_offset, frozen, places=4)
        self.assertEqual(tracker.public(20)["offset_state"], "frozen")
        self.assertGreater(tracker.y, 0)
        tracker.recalibrate_offset()
        self.assertFalse(tracker.offset_locked)
        self.assertEqual(tracker.public(21)["offset_state"], "unset")
        self.assertEqual(tracker.frame_id, frame)
        again = [_orientation(30, 0), _steps(30, 0)]
        for index in range(1, 7):
            again.append(_accel(30 + index - 0.1, 3.0, 0.0, 0.0))
            again.append(_steps(30 + index, index))
        tracker.ingest(_message(*again, session="walk-2"), now=40)
        self.assertTrue(tracker.offset_locked)
        self.assertAlmostEqual(abs(signed_diff(tracker.pocket_offset, frozen)), 180, places=3)

    def test_a_spinning_phone_does_not_take_a_step(self) -> None:
        tracker = BodyTracker(step_length=1)
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(8):
            stamp = 1.0 + index * 0.05
            readings.append(_orientation(stamp, 0))
            readings.append({"name": "gyroscope", "time": int(stamp * 1e9), "values": {"x": 0, "y": 0, "z": 2.0}})
        readings.append(_steps(1.4, 1))
        tracker.ingest(_message(*readings), now=2)
        self.assertEqual(tracker.steps, 0)
        self.assertAlmostEqual(tracker.x, 0)
        self.assertAlmostEqual(tracker.y, 0)
        quiet = [_orientation(2, 0)]
        for index in range(8):
            stamp = 2.0 + index * 0.05
            quiet.append({"name": "gyroscope", "time": int(stamp * 1e9), "values": {"x": 0, "y": 0, "z": 0}})
        quiet.append(_steps(2.5, 2))
        tracker.ingest(_message(*quiet), now=3)
        self.assertEqual(tracker.steps, 1)
        self.assertGreater(math.hypot(tracker.x, tracker.y), 0.5)

    def test_pocketing_the_phone_does_not_take_a_step(self) -> None:
        tracker = BodyTracker(step_length=1)
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(8):
            stamp = index * 0.1
            readings.append(_orientation(stamp, 0, -math.pi / 2 * index / 7))
        readings.append(_steps(0.75, 1))
        tracker.ingest(_message(*readings), now=1)
        self.assertEqual(tracker.steps, 0)
        settled = [_steps(2, 1)]
        for index in range(4):
            settled.append(_orientation(2 + index * 0.2, 0, -math.pi / 2))
        settled.append(_steps(2.8, 2))
        tracker.ingest(_message(*settled), now=3)
        self.assertEqual(tracker.steps, 1)

    def test_accel_steps_when_pedometer_is_absent(self) -> None:
        tracker = BodyTracker(step_length=0.5)
        tracker.ingest(_message(_orientation(0, 0)), now=0)
        for index in range(4):
            second = index * 0.5
            tracker.ingest(_message(_accel(second, 0.2, 0.2, 0.2)), now=second)
            tracker.ingest(_message(_accel(second + 0.2, 4.0, 0.0, 0.0)), now=second + 0.2)
        self.assertEqual(tracker.steps, 4)

    def test_pedometer_wins_over_acceleration(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(_message(_orientation(0, 0), _steps(0, 3)), now=0)
        tracker.ingest(_message(_steps(1, 4), _accel(1, 6, 0, 0)), now=1)
        self.assertEqual(tracker.steps, 1)

    def test_loop_closure(self) -> None:
        with TemporaryDirectory() as folder:
            tracker = BodyTracker(step_length=1, log_dir=Path(folder))
            tracker.ingest(_message(_orientation(0, 0), _steps(0, 0), session="loop-a"), now=0)
            tracker.loop_start()
            tracker.ingest(_message(_steps(1, 4), session="loop-a"), now=1)
            # Turn 180° before the walk back, so those steps use the new heading.
            # The return waits out the two-second heading hold, then walks back.
            tracker.ingest(_message(_orientation(1.2, math.pi), session="loop-a"), now=1.2)
            tracker.ingest(_message(_orientation(3.2, math.pi), _steps(3.2, 4), session="loop-a"), now=3.2)
            tracker.ingest(_message(_steps(4.2, 8), session="loop-a"), now=4.2)
            report = tracker.loop_end()
            self.assertEqual(report["steps"], 8)
            self.assertAlmostEqual(report["path_m"], 8, places=4)
            self.assertLess(report["closure_m"], 0.05)
            log = Path(folder) / "loop-a.jsonl"
            events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertIn("session", events)
            self.assertIn("loop_end", events)
            self.assertIn("step", events)

    def test_calibrate_step_length(self) -> None:
        tracker = BodyTracker(step_length=0.7)
        tracker.ingest(_message(_orientation(0, 0), _steps(0, 0)), now=0)
        tracker.arm_calibration()
        for count in range(1, 11):
            tracker.ingest(_message(_steps(count, count)), now=count)
        self.assertAlmostEqual(tracker.calibrate(7.5), 0.75)

    def test_heading_swing_counts_the_step_without_moving(self) -> None:
        tracker = BodyTracker(step_length=1)
        tracker.ingest(_message(_orientation(0, 0), _steps(0, 0)), now=0)
        held = tracker.pocket_offset
        tracker.ingest(_message(_orientation(2.0, math.radians(70)), _steps(2.0, 1)), now=2)
        public = tracker.public(2)
        self.assertEqual(tracker.steps, 1)
        self.assertAlmostEqual(tracker.x, 0.0)
        self.assertAlmostEqual(tracker.y, 0.0)
        self.assertEqual(tracker.path_length, 0.0)
        self.assertEqual(len(tracker.path), 1)
        self.assertFalse(public["advancing"])
        self.assertEqual(public["hold_reason"], "turning")
        self.assertFalse(tracker.offset_locked)
        self.assertEqual(tracker.pocket_offset, held)
        self.assertEqual(public["offset_state"], "unset")

    def test_batched_steps_are_gated_at_each_sample_time(self) -> None:
        tracker = BodyTracker(step_length=1)
        tracker.ingest(_message(_orientation(0, 0), _steps(0, 0)), now=0)
        readings = []
        for index in range(80):
            second = 0.4 + index * 0.02
            readings.append(_accel(second, 0.0, 0.0, 2.0 * math.sin(2 * math.pi * 2.0 * second)))
        readings.append(_orientation(2.0, 0))
        readings.append(_orientation(3.2, math.radians(80)))
        readings.append(_steps(3.6, 2))
        tracker.ingest(_message(*readings), now=4)
        self.assertEqual(tracker.steps, 2)
        self.assertAlmostEqual(tracker.path_length, 1.0, places=4)
        self.assertAlmostEqual(math.hypot(tracker.x, tracker.y), 1.0, places=4)
        self.assertFalse(tracker.public(4)["advancing"])
        self.assertEqual(tracker.public(4)["hold_reason"], "turning")

    def test_step_without_walking_rhythm_does_not_move(self) -> None:
        tracker = BodyTracker(step_length=1)
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(80):
            readings.append(_accel(index * 0.02, 0.0, 0.0, 0.0))
        readings.append(_steps(1.6, 1))
        tracker.ingest(_message(*readings), now=2)
        public = tracker.public(2)
        self.assertEqual(tracker.steps, 1)
        self.assertAlmostEqual(tracker.x, 0.0)
        self.assertAlmostEqual(tracker.y, 0.0)
        self.assertEqual(tracker.path_length, 0.0)
        self.assertFalse(public["advancing"])
        self.assertEqual(public["hold_reason"], "no walking rhythm")
        self.assertFalse(tracker.offset_locked)

    def test_rhythmic_walk_still_advances(self) -> None:
        tracker = BodyTracker(step_length=0.75)
        readings = [_orientation(0, 0), _steps(0, 0)]
        for index in range(100):
            second = index * 0.02
            readings.append(_accel(second, 0.0, 0.0, 2.0 * math.sin(2 * math.pi * 2.0 * second)))
        readings.append(_steps(2.0, 1))
        tracker.ingest(_message(*readings), now=2)
        public = tracker.public(2)
        self.assertEqual(tracker.steps, 1)
        self.assertGreater(math.hypot(tracker.x, tracker.y), 0.5)
        self.assertAlmostEqual(tracker.path_length, 0.75, places=4)
        self.assertTrue(public["advancing"])
        self.assertEqual(public["hold_reason"], "")

    def test_packet_rate_and_ignored_layout(self) -> None:
        tracker = BodyTracker()
        public = tracker.ingest({"messageId": 1, "sessionId": "s", "payload": []}, now=10)
        tracker.ingest({"not": "a documented reading"}, now=10.5)
        self.assertEqual(public["steps"], 0)
        self.assertGreater(tracker.public(11)["packet_rate"], 0)
        self.assertIsNone(tracker.ingest([], now=12)["heading"])


class SampleTests(unittest.TestCase):
    def test_each_step_uses_its_own_heading(self) -> None:
        tracker = BodyTracker(step_length=1)
        tracker.ingest(
            _message(
                {"name": "compass", "time": 0, "values": {"magneticBearing": 0}},
                {"name": "gyroscope", "time": 0, "values": {"x": 0, "y": 0, "z": 0}},
                {"name": "pedometer", "time": 0, "values": {"steps": 0}},
                {"name": "gyroscope", "time": int(0.5e9), "values": {"x": 0, "y": 0, "z": 1}},
                {"name": "pedometer", "time": int(0.25e9), "values": {"steps": 1}},
                {"name": "pedometer", "time": int(0.5e9), "values": {"steps": 2}},
            ),
            now=1,
        )
        self.assertEqual(tracker.steps, 2)
        first = tracker.path[1]
        second = tracker.path[2]
        self.assertGreater(abs(first[0] - second[0]) + abs(first[1] - second[1]), 0.05)

    def test_compass_corrects_slowly(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(_message({"name": "compass", "time": 0, "values": {"magneticBearing": 0}}), now=0)
        tracker.ingest(
            _message({"name": "compass", "time": int(0.1e9), "values": {"magneticBearing": 100}}),
            now=0.1,
        )
        self.assertAlmostEqual(tracker.phone_heading, 2.0, places=5)

    def test_gyro_turn_is_continuous(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(_message({"name": "compass", "time": 0, "values": {"magneticBearing": 10}}), now=0)
        tracker.ingest(_message({"name": "gyroscope", "time": 0, "values": {"x": 0, "y": 0, "z": 0}}), now=0)
        tracker.ingest(
            _message({"name": "gyroscope", "time": int(0.1e9), "values": {"x": 0, "y": 0, "z": 1}}),
            now=0.1,
        )
        self.assertAlmostEqual(tracker.phone_heading, 10 - math.degrees(0.1), places=4)

    def test_gap_breaks_the_path(self) -> None:
        tracker = BodyTracker(step_length=1)
        tracker.ingest(
            _message(
                {"name": "compass", "time": 0, "values": {"magneticBearing": 0}},
                {"name": "pedometer", "time": int(0.2e9), "values": {"steps": 0}},
                {"name": "pedometer", "time": int(0.4e9), "values": {"steps": 1}},
            ),
            now=0,
        )
        tracker.ingest(
            _message(
                {"name": "compass", "time": int(2.0e9), "values": {"magneticBearing": 0}},
                {"name": "pedometer", "time": int(2.2e9), "values": {"steps": 2}},
            ),
            now=2.2,
        )
        self.assertEqual(tracker.path[-1][2], 1)
        self.assertTrue(tracker.stalls)
        self.assertIn("stopped sampling", tracker.stalls[-1]["reason"])

    def test_sample_rate_counts_samples_not_packets(self) -> None:
        readings = [
            {"name": "accelerometer", "time": int((index + 1) * 0.02e9), "values": {"x": 0, "y": 0, "z": 0}}
            for index in range(51)
        ]
        public = BodyTracker().ingest(_message(*readings), now=1)
        self.assertGreater(public["sample_rates"]["accelerometer"], 40)
        self.assertEqual(public["packet_rate"], 0.5)
        self.assertAlmostEqual(public["sample_gap_ms"]["accelerometer"], 20.0, places=1)

    def test_string_nanosecond_time_is_per_sample(self) -> None:
        readings = []
        for index in range(20):
            stamp = str(1_700_000_000_000_000_000 + index * 10_000_000)
            readings.append({"name": "accelerometer", "time": stamp, "values": {"x": 0, "y": 0, "z": 0}})
        public = BodyTracker().ingest(_message(*readings), now=50)
        self.assertAlmostEqual(public["sample_gap_ms"]["accelerometer"], 10.0, places=1)
        self.assertAlmostEqual(public["sample_rates"]["accelerometer"], 100.0, delta=1)

    def test_missing_time_is_spaced_not_stamped_with_arrival(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(
            _message(
                {"name": "compass", "time": 0, "values": {"magneticBearing": 0}},
                {"name": "gyroscope", "time": 0, "values": {"x": 0, "y": 0, "z": 0}},
            ),
            now=9,
        )
        tracker.ingest(
            _message(
                {"name": "gyroscope", "values": {"x": 0, "y": 0, "z": 1}},
                {"name": "gyroscope", "values": {"x": 0, "y": 0, "z": 1}},
            ),
            now=9,
        )
        self.assertAlmostEqual(signed_diff(tracker.phone_heading, 0), -math.degrees(0.02), places=3)

    def test_quaternion_heading_survives_a_vertical_turn(self) -> None:
        tracker = BodyTracker()
        pitch = -math.pi / 2 + 0.02
        previous = None
        for step in range(0, 37):
            yaw = math.radians(step * 10)
            tracker.ingest(_message(_orientation(step * 0.05, yaw, pitch)), now=step * 0.05)
            heading = tracker.phone_heading
            self.assertEqual(tracker.public()["heading_source"], "orientation")
            if previous is not None:
                self.assertAlmostEqual(signed_diff(heading, previous), -10, places=3)
            previous = heading
        self.assertAlmostEqual(signed_diff(tracker.phone_heading, 0), 0, places=3)

    def test_gyro_and_compass_do_not_override_orientation(self) -> None:
        tracker = BodyTracker()
        tracker.ingest(_message(_orientation(0, 0)), now=0)
        held = tracker.phone_heading
        tracker.ingest(
            _message(
                {"name": "gyroscope", "time": int(0.1e9), "values": {"x": 0, "y": 0, "z": 5}},
                {"name": "compass", "time": int(0.1e9), "values": {"magneticBearing": 10}},
            ),
            now=0.1,
        )
        self.assertAlmostEqual(tracker.phone_heading, held, places=5)
        self.assertEqual(tracker.public()["heading_source"], "orientation")


class GuideTests(unittest.TestCase):
    def test_pulses_speed_up_then_burst(self) -> None:
        far = guide_interval(8, 0)
        mid = guide_interval(4, 170)
        near = guide_interval(1.5, 170)
        arrived = guide_interval(0.4, 180)
        wrapped = guide_interval(8, 350)
        same = guide_interval(8, -10)
        self.assertEqual(mid["mode"], "away")
        self.assertLess(arrived["interval_s"], near["interval_s"])
        self.assertLess(near["interval_s"], mid["interval_s"])
        self.assertLess(mid["interval_s"], far["interval_s"])
        self.assertEqual(arrived["mode"], "arrival")
        self.assertEqual(wrapped["interval_s"], same["interval_s"])
        self.assertTrue(wrapped["facing"])
        self.assertFalse(guide_interval(2, 90)["facing"])
        # A close miss still buzzes faster than a far hit.
        self.assertLess(guide_interval(1.5, 90)["interval_s"], guide_interval(6, 0)["interval_s"])

    def test_replay_image_and_animation(self) -> None:
        from ring.body_replay import render_replay

        track = [
            {"x": index * 0.7, "y": 0.2 * index, "heading": 20, "distance_m": 8 - index, "bearing": 40}
            for index in range(6)
        ]
        png, gif = render_replay(track, (5.0, 2.0), "Keys")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertTrue(gif.startswith(b"GIF"))

    def test_push_paths_are_open(self) -> None:
        self.assertTrue(phone_push_open(PUSH_PATH, "POST"))
        self.assertTrue(phone_push_open(BODY_PAGE, "GET"))
        self.assertTrue(phone_push_open("/api/body/live", "GET"))
        self.assertTrue(phone_push_open("/api/body/save", "POST"))
        self.assertTrue(phone_push_open("/api/body/find", "POST"))
        self.assertFalse(phone_push_open("/api/items", "POST"))
        self.assertFalse(phone_push_open(PUSH_PATH, "GET"))


if __name__ == "__main__":
    unittest.main()
