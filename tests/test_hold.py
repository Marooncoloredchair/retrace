"""Synthetic cover traces for the visual hold detector.

These traces are not the wear-test marks. A finger that is already the baseline
must not count as a held object.
"""

from __future__ import annotations

import unittest

from ring.config import Config
from ring.hold import HoldDetector


def _feed(detector: HoldDetector, samples: list[tuple[float, float, float]]):
    events = []
    for cover, now, gyro in samples:
        event = detector.update(cover, now, int(now * 1000), gyro)
        if event is not None:
            events.append(event)
    return events


class HoldDetectorTest(unittest.TestCase):
    def test_short_baseline_does_not_swallow_a_real_step(self) -> None:
        """A step that lasts past 1s must confirm even when only a few calm frames precede it.

        Folding the step into the median made the rise disappear before the timer elapsed.
        """
        detector = HoldDetector(Config())
        self.assertEqual(_feed(detector, [(0.15, t, 0.0) for t in (0.0, 0.25, 0.5)]), [])
        entered = None
        for t in (0.75, 1.00, 1.25, 1.50, 1.75, 2.00):
            self.assertIsNone(detector.update(0.70, t, int(t * 1000), 0.0))
            if detector.state == HoldDetector.HOLDING and entered is None:
                entered = t
        self.assertEqual(entered, 1.75)
        self.assertEqual(detector.state, HoldDetector.HOLDING)
        event = detector.update(0.15, 2.5, 2500, 0.0)
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "hold")
        self.assertEqual(event.grasp_unix_ms, 750)
        self.assertAlmostEqual(event.baseline, 0.15)
        self.assertGreaterEqual(event.peak_cover, 0.7)
        self.assertEqual(detector.state, HoldDetector.FREE)

    def test_cover_that_is_already_high_does_not_fire(self) -> None:
        detector = HoldDetector(Config())
        events = _feed(detector, [(0.92, i * 0.25, 0.0) for i in range(16)])
        self.assertEqual(events, [])
        self.assertEqual(detector.state, HoldDetector.FREE)

    def test_rise_shorter_than_one_second_does_not_fire(self) -> None:
        detector = HoldDetector(Config())
        _feed(detector, [(0.2, t, 0.0) for t in (0.0, 0.25, 0.5)])
        events = _feed(detector, [(0.8, 0.75, 0.0), (0.8, 1.05, 0.0), (0.2, 1.2, 0.0)])
        self.assertEqual(events, [])
        self.assertEqual(detector.state, HoldDetector.FREE)

    def test_fast_gyro_does_not_start_a_grasp(self) -> None:
        detector = HoldDetector(Config())
        _feed(detector, [(0.2, t, 0.0) for t in (0.0, 0.25, 0.5)])
        events = _feed(detector, [(0.9, 0.75 + i * 0.25, 200.0) for i in range(8)])
        self.assertEqual(events, [])
        self.assertEqual(detector.state, HoldDetector.FREE)

    def test_rise_below_the_margin_does_not_fire(self) -> None:
        detector = HoldDetector(Config())
        _feed(detector, [(0.50, t, 0.0) for t in (0.0, 0.25, 0.5, 0.75)])
        events = _feed(detector, [(0.69, 1.0 + i * 0.25, 0.0) for i in range(8)])
        self.assertEqual(events, [])
        self.assertEqual(detector.state, HoldDetector.FREE)

    def test_cover_after_a_long_gap_is_a_new_baseline(self) -> None:
        detector = HoldDetector(Config())
        _feed(detector, [(0.2, t, 0.0) for t in (0.0, 0.3, 0.6)])
        events = _feed(detector, [(1.0, 40.0 + i * 0.3, 0.0) for i in range(8)])
        self.assertEqual(events, [])
        self.assertEqual(detector.state, HoldDetector.FREE)
