"""The whole loop on drawn pictures: memory, save, breadcrumbs, find, vibration requests.

"Walking" here is sliding a camera along a drawn wall. These tests show that
the logic does what it says. They say nothing about how well a real ring
recognises a real room.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from ring.config import Config
from ring.engine import UserError
from ring.place_find import PlaceFindState, PlaceFinder
from ring.store import Store
from ring.engine import Engine
from tests.ring_sim import Rig, StubLink, World, blurred, covered, hazy


def walk_back(rig: Rig, world: World, from_step: int, to_step: int = 0, frames_per_step: int = 3) -> list[dict]:
    """Return along the wall. Gives the find state after every frame."""
    states = []
    for step in range(from_step, to_step - 1, -1):
        for _ in range(frames_per_step):
            rig.show(world.view(step))
            state = rig.find_state()
            states.append(state)
            if state["status"] == "ARRIVED":
                return states
    return states


class MemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(21)
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_good_frames_are_kept_with_time_and_imu(self) -> None:
        state = self.rig.show(self.world.view(0), gyro_dps=12.0)
        self.assertEqual(state["frames"]["last"]["verdict"], "landmark")
        frame = self.rig.store.recent_keyframes(1)[0]
        self.assertEqual(frame["unix_ms"], int(self.rig.wall() * 1000))
        self.assertEqual(frame["imu"]["at_capture"]["gx_dps"], 12.0)
        self.assertEqual(frame["imu"]["since_last_kept"]["samples"], 1)
        self.assertIsNotNone(self.rig.store.jpeg(frame["id"]))

    def test_blurry_covered_dark_and_fast_frames_are_not_landmarks_but_may_be_observed(self) -> None:
        view = self.world.view(0)
        # Observations continue for soft / covered views; they must not become landmarks.
        hazy_state = self.rig.show(hazy(view))
        self.assertIn(hazy_state["frames"]["last"]["verdict"], ("blurry", "observed", "few_features", "covered"))
        self.assertFalse(hazy_state["frames"]["last"]["landmark"])
        covered_state = self.rig.show(covered(view, 1.0))
        self.assertFalse(covered_state["frames"]["last"]["landmark"])
        self.assertIn(covered_state["frames"]["last"]["verdict"], ("obstructed", "observed", "covered", "few_features"))
        # Near-black is skipped for observation; dim-but-visible frames are still remembered.
        near_black = np.full_like(view, 3)
        self.assertEqual(self.rig.show(near_black)["frames"]["last"]["verdict"], "dark")
        fast = self.rig.show(view, gyro_dps=400.0)
        self.assertIn(fast["frames"]["last"]["verdict"], ("fast_motion", "observed", "landmark"))
        if fast["frames"]["last"]["verdict"] == "fast_motion":
            self.assertFalse(fast["frames"]["last"]["landmark"])
        self.assertEqual(self.rig.engine.landmarks, 0)

    def test_the_same_view_is_kept_once(self) -> None:
        for _ in range(6):
            state = self.rig.show(self.world.view(4))
        self.assertEqual(self.rig.engine.landmarks, 1)
        self.assertGreaterEqual(state["frames"]["rejected"].get("duplicate", 0), 1)
        self.rig.show(self.world.view(5))
        self.assertEqual(self.rig.engine.landmarks, 2)
        self.rig.show(self.world.view(4))  # looking back at a view kept a moment ago
        self.assertEqual(self.rig.engine.landmarks, 2)

    def test_frames_closer_together_than_the_gap_are_not_kept(self) -> None:
        self.rig.show(self.world.view(0))
        state = self.rig.show(self.world.view(3), dt=0.1)
        self.assertEqual(state["frames"]["last"]["verdict"], "too_soon")

    def test_a_picture_that_cannot_be_decoded_is_counted_and_skipped(self) -> None:
        state = self.rig.show(b"\xff\xd8 this is not a jpeg \xff\xd9")
        self.assertEqual(state["frames"]["rejected"], {"undecodable": 1})


class MemoryLimitTest(unittest.TestCase):
    def test_rolling_memory_stays_within_its_frame_limit_while_item_pictures_stay(self) -> None:
        world = World(22, steps=60)
        rig = Rig(memory_max_frames=10, trail_max_breadcrumbs=6)
        self.addCleanup(rig.close)
        item_id = rig.save_at_spot(world)
        rig.walk_away(world, 50)
        rig.wait(10.5)
        usage = rig.store.usage()
        self.assertLessEqual(usage["rolling_frames"], 10)
        item = rig.store.item(item_id)
        self.assertEqual(len(item["breadcrumbs"]), 6)
        pinned_ids = set(item["references"]) | set(item.get("context") or []) | set(item["breadcrumbs"])
        self.assertEqual(usage["item_frames"], len(pinned_ids))
        for keyframe_id in pinned_ids:
            self.assertIsNotNone(rig.store.jpeg(keyframe_id))
        self.assertEqual(len(list((rig.folder / "frames").glob("*.jpg"))), usage["rolling_frames"] + usage["item_frames"])
        self.assertIn("full at 6 breadcrumbs", item["trail_note"])
    def test_trail_closes_at_its_time_limit(self) -> None:
        world = World(22, steps=20)
        rig = Rig(trail_max_minutes=0.1)
        self.addCleanup(rig.close)
        item_id = rig.save_at_spot(world)
        rig.walk_away(world, 4)
        rig.wait(7.0)
        rig.walk_away(world, 4, start=8.0)
        item = rig.store.item(item_id)
        self.assertEqual(len(item["breadcrumbs"]), 4)
        self.assertIn("time limit", item["trail_note"])


class SaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(23)
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_save_uses_several_frames_from_before_and_after_the_button(self) -> None:
        item_id = self.rig.save_at_spot(self.world, "  Keys  ")
        item = self.rig.store.item(item_id)
        self.assertEqual(item["name"], "Keys")
        self.assertEqual(len(item["references"]), 3)  # two from the recent memory, one more while saving
        self.assertEqual(item["trail_state"], "recording")
        self.assertEqual(len(set(item["references"])), 3)

    def test_save_after_holding_still_uses_the_frame_kept_earlier(self) -> None:
        for _ in range(40):  # 20 s on one view: the only kept frame is older than the 6 s look-back
            self.rig.show(self.world.view(0))
        item_id = self.rig.engine.save_item("Keys")["item_id"]
        self.assertEqual(len(self.rig.store.item(item_id)["references"]), 1)

    def test_save_is_refused_without_a_name(self) -> None:
        self.rig.show(self.world.view(0))
        for name in ("   ", "x" * 41):
            with self.assertRaises(UserError) as raised:
                self.rig.engine.save_item(name)
            self.assertEqual(raised.exception.code, "invalid_name")
            self.assertIn("Invalid item name", str(raised.exception))
        self.assertEqual(self.rig.store.items(), [])

    def test_save_still_works_when_the_ring_is_disconnected(self) -> None:
        self.rig.show(self.world.view(0))
        self.rig.link.connected = False
        answer = self.rig.engine.save_item("Keys")
        self.assertEqual(answer["state"], "capturing")
        self.assertEqual(self.rig.store.item(answer["item_id"])["name"], "Keys")

    def test_a_second_save_while_one_is_running_is_refused_with_a_reason(self) -> None:
        self.rig.show(self.world.view(0))
        self.rig.engine.save_item("Keys")
        with self.assertRaises(UserError) as raised:
            self.rig.engine.save_item("Wallet")
        self.assertEqual(raised.exception.code, "busy")

    def test_item_limit(self) -> None:
        rig = Rig(max_items=2)
        self.addCleanup(rig.close)
        rig.show(self.world.view(0))
        for name in ("Keys", "Wallet"):
            rig.engine.save_item(name)
            rig.wait(rig.config.save_capture_s + 0.1)
        with self.assertRaises(UserError):
            rig.engine.save_item("Phone")


class AlwaysSaveTest(unittest.TestCase):
    """Save must persist even when the camera cannot see well.

    This is the product failure from 2026-11-13: Save refused because the live
    view had too few ORB points. The event must be remembered anyway.
    """

    def setUp(self) -> None:
        self.world = World(27)
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_save_with_zero_orb_features_still_creates_the_item(self) -> None:
        blank = np.full((240, 320, 3), 40, dtype=np.uint8)  # near-black, almost no ORB points
        for _ in range(6):
            self.rig.show(blank)
        answer = self.rig.engine.save_item("Keys")
        self.assertEqual(answer["state"], "capturing")
        item = self.rig.store.item(answer["item_id"])
        self.assertEqual(item["name"], "Keys")
        self.assertIn(item["anchor_quality"], ("missing", "limited"))
        self.assertFalse(self.rig.engine.publish()["items"][0]["locatable"] and item["anchor_quality"] == "ready")

    def test_save_with_finger_occlusion_still_creates_the_item(self) -> None:
        for _ in range(8):
            self.rig.show(covered(self.world.view(0), 1.0))
        answer = self.rig.engine.save_item("Keys")
        item = self.rig.store.item(answer["item_id"])
        self.assertEqual(item["name"], "Keys")
        self.assertGreaterEqual(len(item["context"]), 1)
        self.assertIn(item["anchor_quality"], ("missing", "limited"))

    def test_save_with_motion_blur_still_creates_the_item(self) -> None:
        for _ in range(6):
            self.rig.show(blurred(self.world.view(0), 8.0), gyro_dps=300.0)
        answer = self.rig.engine.save_item("Keys")
        self.assertIsNotNone(answer["item_id"])
        self.assertEqual(self.rig.store.item(answer["item_id"])["name"], "Keys")

    def test_observations_around_save_are_preserved_and_anchors_can_arrive_later(self) -> None:
        for _ in range(10):
            self.rig.show(covered(self.world.view(0), 1.0))
        answer = self.rig.engine.save_item("Keys")
        item_id = answer["item_id"]
        self.assertIn(self.rig.store.item(item_id)["anchor_quality"], ("missing", "limited"))
        # Clearer views after Save become anchors.
        for step in (1.0, 2.0, 3.0):
            self.rig.show(self.world.view(step))
        self.rig.wait(self.rig.config.save_capture_s + 0.1)
        item = self.rig.store.item(item_id)
        self.assertEqual(item["trail_state"], "recording")
        self.assertGreaterEqual(len(item["context"]), 1)
        if item["references"]:
            self.assertEqual(item["anchor_quality"], "ready")

    def test_item_survives_restart_even_with_limited_anchor(self) -> None:
        for _ in range(6):
            self.rig.show(covered(self.world.view(0), 1.0))
        item_id = self.rig.engine.save_item("Keys")["item_id"]
        self.rig.wait(self.rig.config.save_capture_s + 0.1)
        folder = self.rig.folder
        self.rig.close(delete=False)
        revived = Rig(folder=folder)
        self.addCleanup(revived.close)
        items = revived.store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], item_id)
        self.assertEqual(items[0]["name"], "Keys")
        self.assertEqual(items[0]["trail_state"], "closed")

    def test_a_disk_that_cannot_create_the_item_is_reported_as_a_filesystem_error(self) -> None:
        def full(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        self.rig.show(self.world.view(0))
        self.rig.store.create_item = full
        with self.assertRaises(UserError) as raised:
            self.rig.engine.save_item("Keys")
        self.assertEqual(raised.exception.code, "storage_error")
        self.assertIn("Filesystem error", str(raised.exception))
        self.assertEqual(self.rig.store.items(), [])


class BreadcrumbTest(unittest.TestCase):
    def test_breadcrumbs_are_in_the_order_they_were_seen(self) -> None:
        world = World(24)
        rig = Rig()
        self.addCleanup(rig.close)
        item_id = rig.save_at_spot(world)
        seen = []
        for index in range(12):
            state = rig.show(world.view(3.0 + index))
            seen.append(state["frames"]["last"]["keyframe_id"])
        detail = rig.engine.item_detail(item_id)
        crumbs = detail["breadcrumbs"]
        self.assertEqual([crumb["keyframe_id"] for crumb in crumbs], seen)
        self.assertEqual([crumb["stage"] for crumb in crumbs], list(range(1, 13)))
        times = [crumb["unix_ms"] for crumb in crumbs]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), 12)
        for crumb in crumbs:
            self.assertNotIn(crumb["keyframe_id"], detail["references"])

    def test_near_black_frames_do_not_become_breadcrumbs(self) -> None:
        world = World(24)
        rig = Rig()
        self.addCleanup(rig.close)
        item_id = rig.save_at_spot(world)
        rig.show(world.view(3))
        before = len(rig.store.item(item_id)["breadcrumbs"])
        blank = np.full((240, 320, 3), 5, dtype=np.uint8)
        for _ in range(6):
            rig.show(blank)
        self.assertEqual(len(rig.store.item(item_id)["breadcrumbs"]), before)
        rig.show(world.view(6))
        self.assertEqual(len(rig.store.item(item_id)["breadcrumbs"]), before + 1)

    def test_two_items_each_get_their_own_ordered_trail(self) -> None:
        world = World(24, steps=40)
        rig = Rig()
        self.addCleanup(rig.close)
        keys = rig.save_at_spot(world, "Keys")
        rig.walk_away(world, 6)
        for step in (9.0, 10.0):
            rig.show(world.view(step))
        wallet = rig.engine.save_item("Wallet")["item_id"]
        rig.show(world.view(11.0))
        rig.wait(rig.config.save_capture_s + 0.1)
        rig.walk_away(world, 5, start=12.0)
        keys_item, wallet_item = rig.store.item(keys), rig.store.item(wallet)
        self.assertEqual(len(keys_item["breadcrumbs"]), 6 + 3 + 5)
        self.assertEqual(len(wallet_item["breadcrumbs"]), 5)
        self.assertEqual(keys_item["breadcrumbs"][-5:], wallet_item["breadcrumbs"])
        self.assertEqual(keys_item["breadcrumbs"], sorted(keys_item["breadcrumbs"]))


class FindTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(25)
        self.rig = Rig()
        self.addCleanup(self.rig.close)
        self.item = self.rig.save_at_spot(self.world)
        self.rig.walk_away(self.world, 24)  # breadcrumb i is the view at step 2 + i

    def test_walking_back_follows_the_trail_and_arrives_once(self) -> None:
        self.rig.engine.start_find(self.item)
        states = walk_back(self.rig, self.world, 26)
        self.assertEqual(states[-1]["status"], "ARRIVED")
        confirmed = [state["confirmed_stage"] for state in states if state["status"] == "MATCHED"]
        self.assertGreater(len(confirmed), 20)
        self.assertEqual(confirmed, sorted(confirmed, reverse=True))  # only ever closer
        self.assertGreaterEqual(confirmed[0], 22)
        sent = self.rig.sent()
        self.assertEqual(sent[-1]["reason"], "arrival")
        self.assertEqual([entry["reason"] for entry in sent].count("arrival"), 1)
        self.assertGreaterEqual(len(sent), 3)
        self.assertEqual(self.rig.link.pulses, len(sent))
        gaps = [later["at"] - earlier["at"] for earlier, later in zip(sent, sent[1:])]
        self.assertTrue(all(gap >= self.rig.config.motor_min_interval_s for gap in gaps), gaps)
        self.assertIsNotNone(self.rig.store.item(self.item)["last_arrived_unix_ms"])

        before = self.rig.link.pulses
        for _ in range(10):  # still at the spot: no second arrival pulse
            self.rig.show(self.world.view(0))
        self.assertEqual(self.rig.link.pulses, before)

    def test_first_match_alone_sends_no_vibration(self) -> None:
        self.rig.engine.start_find(self.item)
        for _ in range(8):
            self.rig.show(self.world.view(22))
        state = self.rig.find_state()
        self.assertEqual(state["status"], "MATCHED")
        self.assertEqual(state["confirmed_stage"], 20)
        self.assertEqual(self.rig.link.pulses, 0)

    def test_better_and_better_matches_of_one_place_send_no_vibration(self) -> None:
        self.rig.engine.start_find(self.item)
        view = self.world.view(22, jitter=0)
        inliers = []
        for sigma in (2.6, 2.2, 1.8, 1.4, 1.0, 0.6, 0.3):
            for _ in range(2):
                self.rig.show(blurred(view, sigma))
                inliers.append(self.rig.find_state()["reading"]["inliers"])
        for _ in range(4):
            self.rig.show(view)
            inliers.append(self.rig.find_state()["reading"]["inliers"])
        self.assertGreater(inliers[-1], 3 * max(1, inliers[0]))  # the match count really did climb
        self.assertEqual(self.rig.find_state()["status"], "MATCHED")
        self.assertEqual(self.rig.link.pulses, 0)
        self.assertEqual(self.rig.outcomes, [])  # not even a request that was blocked

    def test_progress_pulses_are_rate_limited_when_moving_fast(self) -> None:
        rig = Rig(progress_step=1)
        self.addCleanup(rig.close)
        item = rig.save_at_spot(self.world)
        rig.walk_away(self.world, 24)
        rig.engine.start_find(item)
        for step in range(26, 8, -1):  # one breadcrumb every 0.6 s
            for _ in range(3):
                rig.show(self.world.view(step), dt=0.2)
        blocked = [entry for entry in rig.outcomes if not entry["sent"]]
        self.assertGreater(len(blocked), 5)
        self.assertTrue(all("too soon" in entry["blocked"] for entry in blocked))
        sent = rig.sent()
        self.assertGreaterEqual(len(sent), 3)
        gaps = [later["at"] - earlier["at"] for earlier, later in zip(sent, sent[1:])]
        self.assertTrue(all(gap >= 3.0 for gap in gaps), gaps)
        self.assertEqual(rig.link.pulses, len(sent))  # the link was asked exactly as often as the governor allowed

    def test_vibration_switched_off_still_tracks_and_arrives_silently(self) -> None:
        self.rig.engine.set_motor_enabled(False)
        self.rig.engine.start_find(self.item)
        states = walk_back(self.rig, self.world, 26)
        self.assertEqual(states[-1]["status"], "ARRIVED")
        self.assertEqual(self.rig.link.pulses, 0)

    def test_unrelated_place_is_unknown_and_never_arrives(self) -> None:
        elsewhere = World(77)
        self.rig.engine.start_find(self.item)
        for step in range(30):
            self.rig.show(elsewhere.view(step % 25))
        state = self.rig.find_state()
        self.assertEqual(state["status"], "UNKNOWN")
        self.assertIsNone(state["confirmed_stage"])
        self.assertEqual(self.rig.link.pulses, 0)

    def test_covered_or_blurry_view_never_arrives(self) -> None:
        self.rig.engine.start_find(self.item)
        for _ in range(10):
            self.rig.show(covered(self.world.view(0), 1.0))
            self.rig.show(blurred(self.world.view(0), 9.0))
        self.assertEqual(self.rig.find_state()["status"], "UNKNOWN")
        self.assertEqual(self.rig.link.pulses, 0)

    def test_a_place_far_along_the_trail_is_matched_but_is_not_arrival(self) -> None:
        self.rig.engine.start_find(self.item)
        for _ in range(20):
            self.rig.show(self.world.view(20))
        state = self.rig.find_state()
        self.assertEqual(state["status"], "MATCHED")
        self.assertEqual(state["confirmed_stage"], 18)
        self.assertEqual(self.rig.link.pulses, 0)

    def test_two_glimpses_of_the_spot_are_not_arrival(self) -> None:
        elsewhere = World(78)
        self.rig.engine.start_find(self.item)
        self.rig.show(self.world.view(0))
        self.rig.show(self.world.view(1))
        for step in range(20):
            self.rig.show(elsewhere.view(step))
        self.assertEqual(self.rig.find_state()["status"], "UNKNOWN")
        self.assertEqual(self.rig.link.pulses, 0)

    def test_lost_then_recovered_without_a_repeat_vibration(self) -> None:
        elsewhere = World(79)
        self.rig.engine.start_find(self.item)
        walk_back(self.rig, self.world, 26, to_step=14)
        self.assertEqual(self.rig.find_state()["status"], "MATCHED")
        reached = self.rig.find_state()["confirmed_stage"]
        pulses = self.rig.link.pulses
        for step in range(12):  # 6 s somewhere else
            self.rig.show(elsewhere.view(step))
        lost = self.rig.find_state()
        self.assertEqual(lost["status"], "UNKNOWN")
        self.assertEqual(lost["confirmed_stage"], reached)  # shown as "last confirmed", with its age
        self.assertGreater(lost["confirmed_age_s"], 4.0)
        self.assertIsNone(lost["matched_keyframe"])
        for _ in range(4):
            self.rig.show(self.world.view(14))
        again = self.rig.find_state()
        self.assertEqual(again["status"], "MATCHED")
        self.assertEqual(self.rig.link.pulses, pulses)
        texts = [notice["text"] for notice in self.rig.engine.notices]
        self.assertTrue(any(text.startswith("Match lost") for text in texts))
        self.assertTrue(any(text.startswith("Match recovered") for text in texts))

    def test_ring_unplugged_during_find_goes_unknown(self) -> None:
        self.rig.engine.start_find(self.item)
        for _ in range(4):
            self.rig.show(self.world.view(20))
        self.assertEqual(self.rig.find_state()["status"], "MATCHED")
        self.rig.link.connected = False
        self.rig.link.events.append({"kind": "disconnected", "detail": "simulated", "unix_ms": 0})
        self.rig.wait(5.0)
        self.assertEqual(self.rig.find_state()["status"], "UNKNOWN")
        self.assertEqual(len(self.rig.store.items()), 1)

    def test_find_ends_by_itself_after_its_time_limit(self) -> None:
        rig = Rig(find_max_minutes=0.05)
        self.addCleanup(rig.close)
        item = rig.save_at_spot(self.world)
        rig.engine.start_find(item)
        rig.wait(4.0)
        self.assertIsNone(rig.find_state())


class AmbiguousTest(unittest.TestCase):
    def test_two_places_that_look_the_same_are_not_confirmed(self) -> None:
        world = World(26, steps=40)
        world.repeat(source_step=8, target_step=24, steps=4)  # steps 24-28 look exactly like steps 8-12
        rig = Rig()
        self.addCleanup(rig.close)
        item = rig.save_at_spot(world)
        rig.walk_away(world, 32)
        rig.engine.start_find(item)

        for _ in range(12):
            rig.show(world.view(26))
        state = rig.find_state()
        self.assertEqual(state["status"], "UNKNOWN")
        self.assertTrue(state["reading"]["ambiguous"])
        self.assertIn("ambiguous", state["reading"]["note"])
        self.assertEqual(rig.link.pulses, 0)

        for _ in range(12):  # the other copy is just as ambiguous
            rig.show(world.view(10))
        self.assertEqual(rig.find_state()["status"], "UNKNOWN")

        for _ in range(5):  # a stretch of wall that exists once is confirmed normally
            rig.show(world.view(18))
        state = rig.find_state()
        self.assertEqual(state["status"], "MATCHED")
        self.assertEqual(state["confirmed_stage"], 16)

    def test_lingering_on_one_scene_does_not_make_it_ambiguous(self) -> None:
        world = World(27, steps=40)
        rig = Rig(duplicate_shift_px=10.0)  # keep frames that barely moved, as a swaying hand would produce
        self.addCleanup(rig.close)
        item = rig.save_at_spot(world)
        rig.walk_away(world, 6)
        for index in range(14):  # 14 breadcrumbs of nearly one scene: each shifted by a quarter step
            rig.show(world.view(9.0 + index * 0.25, jitter=0))
        rig.walk_away(world, 6, start=13.0)
        self.assertEqual(len(rig.store.item(item)["breadcrumbs"]), 26)
        rig.engine.start_find(item)
        for _ in range(5):
            rig.show(world.view(10.5))
        state = rig.find_state()
        self.assertEqual(state["status"], "MATCHED")
        self.assertFalse(state["reading"]["ambiguous"])
        self.assertTrue(7 <= state["confirmed_stage"] <= 20, state["confirmed_stage"])


class RestartTest(unittest.TestCase):
    def test_item_saved_before_a_restart_can_be_found_after_it(self) -> None:
        world = World(28)
        first = Rig()
        item = first.save_at_spot(world, "Keys")
        first.walk_away(world, 12)
        folder = first.folder
        first.close(delete=False)

        second = Rig(folder=folder)
        self.addCleanup(lambda: second.close(delete=True))
        items = second.engine.publish()["items"]
        self.assertEqual([(entry["name"], entry["references"], entry["breadcrumbs"]) for entry in items], [("Keys", 3, 12)])
        self.assertEqual(items[0]["trail_state"], "closed")
        self.assertIn("app last stopped", items[0]["trail_note"])
        second.show(world.view(20))  # new frames after the restart do not join the old trail
        self.assertEqual(len(second.store.item(item)["breadcrumbs"]), 12)

        second.engine.start_find(item)
        states = walk_back(second, world, 14)
        self.assertEqual(states[-1]["status"], "ARRIVED")
        self.assertEqual(second.sent()[-1]["reason"], "arrival")

    def test_delete_removes_the_item_and_stops_its_find(self) -> None:
        world = World(28)
        rig = Rig()
        self.addCleanup(rig.close)
        item = rig.save_at_spot(world)
        rig.walk_away(world, 5)
        rig.engine.start_find(item)
        rig.engine.delete_item(item)
        self.assertIsNone(rig.find_state())
        self.assertEqual(rig.store.items(), [])
        with self.assertRaises(UserError):
            rig.engine.start_find(item)


class FindSessionLogTest(unittest.TestCase):
    def test_place_backend_keeps_the_old_cap_until_a_find_starts(self) -> None:
        rig = Rig(find_backend="place")
        self.addCleanup(rig.close)
        motor = rig.engine.motor
        self.assertFalse(motor.finding)
        self.assertEqual(motor.min_interval_s, rig.config.motor_min_interval_s)
        self.assertEqual(motor.find_min_interval_s, rig.config.find_map_min_pulse_interval)
        sent = []
        for _ in range(9):
            sent.append(motor.request("manual test")["sent"])
            rig.now += 3.0
        self.assertEqual(sent, [True] * 8 + [False])
        self.assertIn("limit of 8 pulses per minute", motor.log[-1]["blocked"])

    def test_session_log_stores_pulse_counts_and_a_row_per_frame(self) -> None:
        rig = Rig(find_backend="place")
        self.addCleanup(rig.close)
        find = {"item_id": 7, "name": "keys", "backend": "place"}
        rig.engine._open_find_session(find)
        self.assertTrue(rig.engine.motor.finding)
        rig.engine._log_find_frame(find, unix_ms=10, passed_gate=False, s_item=None, s_bg=None, heat=None)
        rig.engine._log_find_frame(find, unix_ms=20, passed_gate=True, s_item=0.8123, s_bg=0.2011, heat=0.457)
        rig.engine._note_find_request(find, {"sent": True, "blocked": ""})
        rig.engine._note_find_request(find, {"sent": False, "blocked": "", "coalesced": True})
        rig.engine._note_find_request(find, {"sent": False, "blocked": "the ring is not connected"})
        saved = json.loads(Path(find["session_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["requested"], 3)
        self.assertEqual(saved["sent"], 1)
        self.assertEqual(saved["blocked"], 1)
        self.assertEqual(saved["frames"][0], {"unix_ms": 10, "passed_gate": False, "s_item": None, "s_bg": None, "heat": None})
        self.assertEqual(saved["frames"][1]["passed_gate"], True)
        self.assertEqual(saved["frames"][1]["s_item"], 0.8123)
        self.assertEqual(saved["frames"][1]["s_bg"], 0.2011)
        self.assertEqual(saved["frames"][1]["heat"], 0.457)
        rig.engine.motor.finding = False

    def test_a_frame_that_fails_the_place_gate_is_not_scored(self) -> None:
        finder = PlaceFinder(Config(), store=None, embedder=None)
        finder.state = PlaceFindState(
            item_id=1,
            name="keys",
            place="",
            grasp_unix_ms=0,
            release_unix_ms=0,
            anchors=[],
            bg_embeddings=[],
            last_seen_keyframe=None,
            started=0.0,
        )
        finder.state.s_item = 0.9
        finder.state.s_bg = 0.4
        finder.state.heat = 0.8
        public = finder.update(jpeg=b"not-a-jpeg", usable=False, unix_ms=1, now=1.0)
        self.assertFalse(public["scored"])
        self.assertTrue(public["silent"])
        self.assertEqual(finder.state.s_item, 0.9)


if __name__ == "__main__":
    unittest.main()
