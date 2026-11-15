"""The same loop on pictures and IMU samples the real ring recorded.

Three recordings exist. They show how the service treats real ring data. They
cannot show that a person walking back to a spot is recognised: no such
recording exists yet.

  ring_tests/20261112-233427-software-check   13 usable pictures of one scene,
                                              a finger over about half the lens
  ring_footage/20261112-235629-first-sample   45 s, 1,249 pictures with a hand
                                              over the lens, 1,248 IMU samples
  ring_tests/20261113-005605-worn-live-view   40 s, 516 pictures from the live
                                              view of the worn ring, taken while
                                              Save Item was failing; no IMU file

The recordings folder is not in git. Tests skip when a recording is absent.
"""

from __future__ import annotations

import csv
import unittest
from pathlib import Path

from tests.ring_sim import Rig, World, imu_sample

ROOT = Path(__file__).resolve().parents[1]
STILL = ROOT / "recordings" / "ring_tests" / "20261112-233427-software-check" / "images"
COVERED = ROOT / "recordings" / "ring_footage" / "20261112-235629-first-sample"
WORN = ROOT / "recordings" / "ring_tests" / "20261113-005605-worn-live-view" / "images"


def covered_recording(every: int = 3) -> list[tuple[bytes, dict | None]]:
    """(jpeg, the IMU sample read just before it) for every n-th recorded frame."""
    from ring.link import framing

    samples = {}
    with (COVERED / "imu.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw = [int(row[name]) for name in ("ax_raw", "ay_raw", "az_raw", "temp_raw", "gx_raw", "gy_raw", "gz_raw")]
            payload = framing.IMU_PACKET.pack(0x68, 0x68, int(row["accel_fs"]), int(row["gyro_fs"]), int(row["board_ms"]), *raw)
            samples[row["sample"]] = framing.decode_imu(payload)
    frames = []
    with (COVERED / "frames.csv").open(encoding="utf-8", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle)):
            if index % every:
                continue
            frames.append(((COVERED / row["file"]).read_bytes(), samples.get(row["imu_sample_before"])))
    return frames


def play(rig: Rig, jpeg: bytes, imu: dict | None, dt: float = 0.11) -> dict:
    rig.now += dt
    rig.seq += 1
    sample = imu or imu_sample()
    sample = {**sample, "host_unix": rig.wall(), "host_mono": rig.now}
    rig.engine.process(
        {"seq": rig.seq, "jpeg": jpeg, "host_mono": rig.now, "host_unix": rig.wall(), "imu": sample, "imu_samples": [sample]}
    )
    rig.engine.tick()
    return rig.engine.publish()


@unittest.skipUnless(STILL.is_dir(), "recording 20261112-233427-software-check is not on this computer")
class RecordedStillSceneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.references = sorted(STILL.glob("ref-*.jpg"))
        self.later = sorted(STILL.glob("test-*.jpg"))
        self.rig = Rig()
        self.addCleanup(self.rig.close)

    def test_real_ring_pictures_pass_the_quality_check(self) -> None:
        verdicts = [self.rig.show(path.read_bytes())["frames"]["last"]["verdict"] for path in self.references + self.later]
        self.assertEqual(set(verdicts) - {"landmark", "observed", "duplicate", "too_soon"}, set(), verdicts)
        self.assertGreaterEqual(verdicts.count("landmark"), 1)

    def test_save_from_real_pictures_then_recognise_later_pictures_of_the_same_scene(self) -> None:
        for path in self.references:
            self.rig.show(path.read_bytes())
        item = self.rig.engine.save_item("Desk")["item_id"]
        self.rig.wait(self.rig.config.save_capture_s + 0.1)
        self.assertGreaterEqual(len(self.rig.store.item(item)["references"]), 1)
        self.assertEqual(self.rig.store.item(item)["anchor_quality"], "ready")
        self.rig.engine.start_find(item)
        statuses = []
        for path in self.later:
            self.rig.show(path.read_bytes())
            statuses.append(self.rig.find_state()["status"])
        self.assertEqual(statuses[0], "UNKNOWN")
        self.assertEqual(statuses[1], "UNKNOWN")
        self.assertEqual(statuses[-1], "ARRIVED")
        self.assertEqual([entry["reason"] for entry in self.rig.sent()], ["arrival"])

    def test_real_scene_is_not_mistaken_for_a_drawn_one(self) -> None:
        world = World(51)
        item = self.rig.save_at_spot(world)
        self.rig.walk_away(world, 10)
        self.rig.engine.start_find(item)
        for path in self.references + self.later:
            self.rig.show(path.read_bytes())
        self.assertEqual(self.rig.find_state()["status"], "UNKNOWN")
        self.assertEqual(self.rig.sent(), [])


@unittest.skipUnless(WORN.is_dir(), "recording 20261113-005605-worn-live-view is not on this computer")
class RecordedWornRingSaveTest(unittest.TestCase):
    """Save Item on the pictures of the session in which it failed on the real ring."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = [path.read_bytes() for path in sorted(WORN.glob("*.jpg"))]
        cls.dt = 40.0 / len(cls.frames)

    def test_every_save_press_persists_even_when_detail_is_low(self) -> None:
        rig = Rig(max_items=100, save_capture_s=0.8)
        self.addCleanup(rig.close)
        saved = []
        for index, jpeg in enumerate(self.frames):
            play(rig, jpeg, None, self.dt)
            if index % 40 == 39 and rig.engine.capture is None:
                answer = rig.engine.save_item(f"Item {index}")
                self.assertIsNotNone(answer["item_id"], answer)
                self.assertEqual(answer["state"], "capturing")
                saved.append(answer["item_id"])
                rig.wait(rig.config.save_capture_s + 0.05)
        self.assertGreaterEqual(len(saved), 8)
        self.assertEqual(len(rig.store.items()), len(saved))
        # Continuous observation memory kept running through the worn footage.
        self.assertGreaterEqual(rig.engine.observed, 100, rig.engine.publish()["frames"])

    def test_occlusion_mask_marks_finger_covered_regions(self) -> None:
        from ring.vision import analyse
        from tests.ring_sim import World, covered, encode

        full = analyse(encode(covered(World(9).view(0), 1.0)), 0, 0)
        self.assertGreaterEqual(full["occlusion"], 0.9)
        sample = analyse(self.frames[2], 0, 0)
        self.assertIsNotNone(sample)
        self.assertGreaterEqual(sample["occlusion"], 0.15, sample)
        self.assertLess(sample["visible_feature_count"], sample["feature_count"])


@unittest.skipUnless((COVERED / "frames.csv").is_file(), "recording 20261112-235629-first-sample is not on this computer")
class RecordedCoveredLensTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = covered_recording()

    def test_covered_lens_footage_is_observed_but_save_still_persists(self) -> None:
        rig = Rig()
        self.addCleanup(rig.close)
        for jpeg, imu in self.frames:
            state = play(rig, jpeg, imu)
        frames = state["frames"]
        self.assertEqual(frames["processed"], len(self.frames))
        # Almost none become landmarks, but observations may still be stored.
        self.assertLessEqual(frames["landmarks"], len(self.frames) // 20, frames)
        answer = rig.engine.save_item("Keys")
        self.assertIsNotNone(answer["item_id"])
        item = rig.store.item(answer["item_id"])
        self.assertEqual(item["name"], "Keys")
        self.assertIn(item["anchor_quality"], ("missing", "limited"))
        logged = sum(len(path.read_text(encoding="utf-8").splitlines()) - 1 for path in (rig.folder / "imu").glob("imu-*.csv"))
        self.assertGreaterEqual(logged, len(self.frames) - 100)

    def test_covered_lens_footage_never_matches_a_saved_item(self) -> None:
        still = sorted(STILL.glob("ref-*.jpg")) if STILL.is_dir() else []
        rig = Rig()
        self.addCleanup(rig.close)
        if still:
            for path in still:
                rig.show(path.read_bytes())
            item = rig.engine.save_item("Desk")["item_id"]
            rig.wait(rig.config.save_capture_s + 0.1)
        else:
            item = rig.save_at_spot(World(52))
        rig.engine.start_find(item)
        statuses = set()
        for jpeg, imu in self.frames:
            play(rig, jpeg, imu)
            statuses.add(rig.find_state()["status"])
        self.assertEqual(statuses, {"UNKNOWN"})
        self.assertEqual(rig.outcomes, [])


if __name__ == "__main__":
    unittest.main()
