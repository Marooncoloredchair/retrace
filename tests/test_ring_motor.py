"""The vibration rate limits. No hardware: the 'send' here only counts."""

from __future__ import annotations

import unittest

from ring.config import Config
from ring.motor import MotorGovernor


class Bench:
    def __init__(self, **settings) -> None:
        self.now = 0.0
        self.sends = 0
        self.connected = True
        options = {"min_interval_s": 3.0, "max_per_minute": 8, "enabled": True, **settings}
        self.motor = MotorGovernor(send=self.send, clock=lambda: self.now, **options)

    def send(self) -> bool:
        if not self.connected:
            return False
        self.sends += 1
        return True


class MotorGovernorTest(unittest.TestCase):
    def test_second_request_inside_the_interval_is_dropped(self) -> None:
        bench = Bench()
        self.assertTrue(bench.motor.request("progress")["sent"])
        bench.now = 2.9
        second = bench.motor.request("progress")
        self.assertFalse(second["sent"])
        self.assertIn("too soon", second["blocked"])
        bench.now = 3.0
        self.assertTrue(bench.motor.request("arrival")["sent"])
        self.assertEqual(bench.sends, 2)

    def test_a_dropped_request_is_never_sent_later(self) -> None:
        bench = Bench()
        bench.motor.request("progress")
        for step in range(1, 20):
            bench.now = step * 0.1
            bench.motor.request("progress")
        bench.now = 100.0
        self.assertEqual(bench.sends, 1)
        self.assertEqual(bench.motor.sent, 1)
        self.assertEqual(bench.motor.blocked, 19)

    def test_per_minute_limit(self) -> None:
        bench = Bench(max_per_minute=4)
        outcomes = []
        for index in range(10):
            bench.now = index * 5.0  # 0, 5, ... 45 s: all inside one minute, all past the interval
            outcomes.append(bench.motor.request("progress")["sent"])
        self.assertEqual(outcomes, [True] * 4 + [False] * 6)
        bench.now = 61.0  # the first pulse is now more than a minute old
        self.assertTrue(bench.motor.request("progress")["sent"])
        self.assertEqual(bench.sends, 5)

    def test_switched_off_sends_nothing(self) -> None:
        bench = Bench(enabled=False)
        outcome = bench.motor.request("arrival")
        self.assertFalse(outcome["sent"])
        self.assertEqual(bench.sends, 0)
        bench.motor.enabled = True
        self.assertTrue(bench.motor.request("arrival")["sent"])

    def test_disconnected_ring_is_not_counted_as_sent_and_does_not_use_up_the_limit(self) -> None:
        bench = Bench()
        bench.connected = False
        outcome = bench.motor.request("arrival")
        self.assertFalse(outcome["sent"])
        self.assertIn("not connected", outcome["blocked"])
        bench.connected = True
        bench.now = 0.5
        self.assertTrue(bench.motor.request("arrival")["sent"])

    def test_no_motor_run_cannot_be_switched_on(self) -> None:
        bench = Bench(locked="ReTrace was started with --no-motor")
        bench.motor.enabled = True
        outcome = bench.motor.request("arrival")
        self.assertFalse(outcome["sent"])
        self.assertEqual(outcome["blocked"], "ReTrace was started with --no-motor")
        self.assertEqual(bench.sends, 0)

    def test_only_known_reasons_are_accepted(self) -> None:
        bench = Bench()
        with self.assertRaises(ValueError):
            bench.motor.request("more matches")

    def test_find_mode_sends_every_tick_and_coalesces_a_request_inside_the_interval(self) -> None:
        bench = Bench(min_interval_s=3.0, max_per_minute=8)
        bench.motor.finding = True
        bench.motor.find_min_interval_s = 0.3
        sent = []
        for index in range(5):
            bench.now = index * 0.3
            sent.append(bench.motor.request("guide")["sent"])
        self.assertEqual(sent, [True] * 5)
        bench.now = 4 * 0.3 + 0.05
        extra = bench.motor.request("guide")
        self.assertFalse(extra["sent"])
        self.assertEqual(extra["blocked"], "")
        self.assertTrue(extra["coalesced"])
        self.assertNotIn("per minute", extra["blocked"])
        self.assertEqual(bench.sends, 5)
        self.assertEqual(bench.motor.blocked, 0)
        # The 8-per-minute cap does not apply: four more ticks make nine sends inside one minute.
        for step in range(1, 5):
            bench.now = 1.2 + step * 0.3
            self.assertTrue(bench.motor.request("guide")["sent"])
        self.assertEqual(bench.sends, 9)
        self.assertEqual(bench.motor.sent, 9)
        self.assertEqual(bench.motor.requested, 10)
        self.assertEqual(bench.motor.blocked, 0)

    def test_outside_find_the_per_minute_cap_still_blocks(self) -> None:
        bench = Bench(min_interval_s=3.0, max_per_minute=8)
        self.assertFalse(bench.motor.finding)
        outcomes = []
        for index in range(9):
            bench.now = index * 3.0
            outcomes.append(bench.motor.request("manual test"))
        self.assertEqual([item["sent"] for item in outcomes], [True] * 8 + [False])
        self.assertIn("limit of 8 pulses per minute", outcomes[8]["blocked"])
        self.assertFalse(outcomes[8]["coalesced"])
        self.assertEqual(bench.sends, 8)
        self.assertEqual(bench.motor.blocked, 1)

    def test_interval_cannot_be_set_below_the_firmware_pulse_and_rest(self) -> None:
        with self.assertRaises(ValueError):
            Config(motor_min_interval_s=0.5).validate()
        with self.assertRaises(ValueError):
            Config(required_frames=1).validate()
        Config().validate()


if __name__ == "__main__":
    unittest.main()
