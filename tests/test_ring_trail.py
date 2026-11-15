"""The matching rules on their own, with made-up comparison results.

Each test feeds the tracker the numbers a frame comparison would produce, so
the rules are checked exactly, without pictures.
"""

from __future__ import annotations

import unittest

from ring.config import Config
from ring.trail import ARRIVED, MATCHED, UNKNOWN, Neighbours, TrailTracker, read_frame


def scores(**by_stage: int) -> dict:
    """scores(s5=40, s0=3) -> stage 5 has 40 agreeing points, the saved spot has 3."""
    return {int(name[1:]): {"inliers": inliers, "keyframe_id": 100 + int(name[1:])} for name, inliers in by_stage.items()}


class Bench:
    def __init__(self, breadcrumbs: int = 40, neighbours: Neighbours | None = None, **settings) -> None:
        self.config = Config(**settings)
        self.neighbours = neighbours
        self.tracker = TrailTracker(breadcrumbs=breadcrumbs, config=self.config, neighbours=neighbours)
        self.now = 0.0
        self.events: list[dict] = []

    def frame(self, dt: float = 0.2, **by_stage: int) -> list[dict]:
        self.now += dt
        reading = read_frame(scores(**by_stage), self.now, self.config, self.neighbours)
        events = self.tracker.update(reading)
        self.events.extend(events)
        return events

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events]


class ReadFrameTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config()

    def test_below_twelve_agreeing_points_names_nothing(self) -> None:
        reading = read_frame(scores(s5=11, s6=9), 0.0, self.config)
        self.assertIsNone(reading.stage)
        self.assertFalse(reading.ambiguous)

    def test_one_clear_stage_is_named(self) -> None:
        reading = read_frame(scores(s5=40, s6=31, s20=3), 0.0, self.config)
        self.assertEqual(reading.stage, 5)

    def test_neighbouring_breadcrumbs_seeing_the_same_scene_are_not_a_conflict(self) -> None:
        reading = read_frame(scores(s5=40, s6=39, s7=38, s4=37), 0.0, self.config)
        self.assertEqual(reading.stage, 5)

    def test_two_distant_stages_that_both_fit_are_ambiguous(self) -> None:
        reading = read_frame(scores(s5=300, s25=290), 0.0, self.config)
        self.assertIsNone(reading.stage)
        self.assertTrue(reading.ambiguous)

    def test_a_distant_verified_match_is_ambiguous_even_when_clearly_weaker(self) -> None:
        # 300 against 150 passes a margin of 8, yet the second place is itself a verified match.
        reading = read_frame(scores(s5=300, s25=150), 0.0, self.config)
        self.assertTrue(reading.ambiguous)
        reading = read_frame(scores(s5=300, s25=20), 0.0, self.config)
        self.assertEqual(reading.stage, 5)

    def test_small_margin_over_a_distant_stage_is_ambiguous(self) -> None:
        reading = read_frame(scores(s5=16, s25=10), 0.0, self.config)
        self.assertTrue(reading.ambiguous)

    def test_a_measured_run_of_one_scene_extends_the_neighbourhood(self) -> None:
        lingering = Neighbours(3, {5: (5, 14), 14: (5, 14)})
        reading = read_frame(scores(s5=200, s14=190), 0.0, self.config, lingering)
        self.assertEqual(reading.stage, 5)
        reading = read_frame(scores(s5=200, s14=190), 0.0, self.config)
        self.assertTrue(reading.ambiguous)

    def test_reference_hit_needs_the_references_themselves(self) -> None:
        reading = read_frame(scores(s0=5, s1=200), 0.0, self.config)
        self.assertEqual(reading.stage, 1)
        self.assertFalse(reading.ref_hit)
        reading = read_frame(scores(s0=60, s1=200), 0.0, self.config)
        self.assertTrue(reading.ref_hit)
        reading = read_frame(scores(s0=60, s30=55), 0.0, self.config)
        self.assertFalse(reading.ref_hit)


class TrackerTest(unittest.TestCase):
    def test_one_or_two_frames_do_not_confirm_and_three_do(self) -> None:
        bench = Bench()
        bench.frame(s20=50)
        bench.frame(s20=50)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        bench.frame(s20=50)
        self.assertEqual(bench.tracker.status, MATCHED)
        self.assertEqual(bench.tracker.confirmed_stage, 20)
        self.assertEqual(bench.kinds(), ["joined"])

    def test_a_distant_match_in_the_window_blocks_confirmation(self) -> None:
        bench = Bench()
        bench.frame(s35=50)
        bench.frame(s20=50)
        bench.frame(s20=50)
        bench.frame(s20=50)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        for _ in range(9):  # the stray frame leaves the 10-frame window
            bench.frame(s20=50)
        self.assertEqual(bench.tracker.status, MATCHED)

    def test_ambiguous_frames_never_confirm(self) -> None:
        bench = Bench()
        for _ in range(30):
            bench.frame(s8=300, s30=290)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        self.assertEqual(bench.events, [])

    def test_rising_match_counts_at_one_place_are_not_progress(self) -> None:
        bench = Bench()
        for inliers in range(12, 400, 6):
            bench.frame(s20=inliers)
        self.assertEqual(bench.tracker.status, MATCHED)
        self.assertEqual(bench.kinds(), ["joined"])
        self.assertEqual(bench.tracker.closest_stage, 20)

    def test_progress_needs_a_closer_stage_by_the_step(self) -> None:
        bench = Bench(progress_step=4)
        for stage in (20, 19, 18, 17):
            for _ in range(3):
                bench.frame(**{f"s{stage}": 50})
        self.assertNotIn("progress", bench.kinds())
        for _ in range(3):
            bench.frame(s16=50)
        self.assertEqual(bench.kinds(), ["joined", "progress"])
        self.assertEqual(bench.events[-1]["stage"], 16)

    def test_moving_away_and_coming_back_to_a_stage_already_reached_is_not_progress(self) -> None:
        bench = Bench(progress_step=2)
        for stage in (20, 18, 16, 18, 20, 18, 16):
            for _ in range(4):
                bench.frame(**{f"s{stage}": 50})
        self.assertEqual(bench.kinds().count("progress"), 2)  # 18 and 16, once each

    def test_a_sudden_big_jump_needs_five_frames(self) -> None:
        bench = Bench()
        for _ in range(3):
            bench.frame(s30=50)
        self.assertEqual(bench.tracker.confirmed_stage, 30)
        for _ in range(10):  # let the stage-30 frames leave the window with frames that name nothing
            bench.frame(s30=2)
        for _ in range(4):
            bench.frame(s5=50)
        self.assertEqual(bench.tracker.confirmed_stage, 30)
        bench.frame(s5=50)
        self.assertEqual(bench.tracker.confirmed_stage, 5)

    def test_lost_after_the_window_passes_without_confirmation_and_recovered_later(self) -> None:
        bench = Bench()
        for _ in range(3):
            bench.frame(s20=50)
        for _ in range(25):
            bench.frame(s20=0)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        self.assertIn("lost", bench.kinds())
        self.assertEqual(bench.tracker.confirmed_stage, 20)  # remembered, shown as "last confirmed"
        for _ in range(3):
            bench.frame(s19=50)
        self.assertEqual(bench.tracker.status, MATCHED)
        self.assertEqual(bench.kinds(), ["joined", "lost", "recovered"])

    def test_tick_expires_a_match_when_no_frames_arrive(self) -> None:
        bench = Bench()
        for _ in range(3):
            bench.frame(s20=50)
        self.assertEqual(bench.tracker.tick(bench.now + 1.0), [])
        events = bench.tracker.tick(bench.now + 4.5)
        self.assertEqual([event["kind"] for event in events], ["lost"])
        self.assertEqual(bench.tracker.status, UNKNOWN)

    def test_arrival_needs_three_reference_frames(self) -> None:
        bench = Bench()
        bench.frame(s0=80)
        bench.frame(s0=80)
        self.assertNotEqual(bench.tracker.status, ARRIVED)
        bench.frame(s0=80)
        self.assertEqual(bench.tracker.status, ARRIVED)
        self.assertEqual(bench.kinds(), ["arrived"])
        bench.frame(s0=80)
        self.assertEqual(bench.kinds(), ["arrived"])  # once only

    def test_breadcrumbs_next_to_the_spot_do_not_count_as_arrival(self) -> None:
        bench = Bench()
        for _ in range(30):
            bench.frame(s1=200, s2=150, s0=4)
        self.assertEqual(bench.tracker.status, MATCHED)
        self.assertNotIn("arrived", bench.kinds())

    def test_no_arrival_while_recent_frames_point_far_away(self) -> None:
        bench = Bench()
        for _ in range(3):
            bench.frame(s30=80)
        for _ in range(3):
            bench.frame(s0=80)
        self.assertNotEqual(bench.tracker.status, ARRIVED)

    def test_no_arrival_when_the_spot_looks_like_a_distant_part_of_the_trail(self) -> None:
        bench = Bench()
        for _ in range(30):
            bench.frame(s0=200, s30=190)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        self.assertEqual(bench.events, [])

    def test_two_reference_frames_then_nothing_never_arrives(self) -> None:
        bench = Bench()
        bench.frame(s0=80)
        bench.frame(s0=80)
        for _ in range(40):
            bench.frame(s0=0)
        self.assertEqual(bench.tracker.status, UNKNOWN)
        self.assertEqual(bench.events, [])


if __name__ == "__main__":
    unittest.main()
