"""Persistence and the storage limits of the visual memory."""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from ring.store import TRAIL_RECORDING, Store
from ring.vision import analyse
from tests.ring_sim import World, encode, imu_sample


class StoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        world = World(3, steps=14)
        cls.results = [analyse(encode(world.view(step)), step + 1, 1000 + step) for step in range(14)]

    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="retrace-store-"))
        self.store = Store(self.folder)

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def add(self, count: int, unix_ms: int = 1_000_000, spacing_ms: int = 1000) -> list[int]:
        return [
            self.store.add_frame(
                self.results[index % len(self.results)],
                unix_ms + index * spacing_ms,
                {"n": index},
                role="landmark",
            )
            for index in range(count)
        ]

    def test_item_and_its_pictures_survive_closing_and_reopening(self) -> None:
        ids = self.add(5)
        item_id = self.store.create_item("Keys", 123)
        for keyframe_id in ids[:2]:
            self.store.append_frame(item_id, "ref", keyframe_id)
        for keyframe_id in ids[2:]:
            self.store.append_frame(item_id, "crumb", keyframe_id)
        self.store.set_trail(item_id, TRAIL_RECORDING)
        before_kp, before_desc = self.store.features(ids[0])
        self.store.close()

        self.store = Store(self.folder)
        items = self.store.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Keys")
        self.assertEqual(items[0]["references"], ids[:2])
        self.assertEqual(items[0]["breadcrumbs"], ids[2:])
        after_kp, after_desc = self.store.features(ids[0])
        self.assertTrue(np.array_equal(before_desc, after_desc))
        self.assertEqual(len(before_kp), len(after_kp))
        self.assertEqual(self.store.jpeg(ids[0]), self.results[0]["jpeg"])
        self.assertEqual(self.store.keyframe(ids[3])["imu"], {"n": 3})

    def test_frame_limit_removes_the_oldest_first(self) -> None:
        ids = self.add(12)
        removed = self.store.prune(max_frames=5, max_minutes=1e6, max_mb=1e6, now_unix_ms=1_100_000)
        self.assertEqual(removed, 7)
        self.assertEqual([frame["id"] for frame in self.store.recent_keyframes(50)], ids[:6:-1])
        for keyframe_id in ids[:7]:
            self.assertFalse((self.folder / "frames" / f"{keyframe_id}.jpg").exists())
            self.assertFalse((self.folder / "features" / f"{keyframe_id}.npz").exists())
        self.assertEqual(self.store.usage()["rolling_frames"], 5)

    def test_size_limit_holds(self) -> None:
        self.add(12)
        one_frame_mb = self.store.usage()["rolling_mb"] / 12
        budget = one_frame_mb * 4.5
        self.store.prune(max_frames=10_000, max_minutes=1e6, max_mb=budget, now_unix_ms=1_100_000)
        usage = self.store.usage()
        self.assertLessEqual(usage["rolling_mb"], budget + 0.01)
        self.assertGreaterEqual(usage["rolling_frames"], 3)
        on_disk = sum(path.stat().st_size for path in self.folder.glob("frames/*")) + sum(
            path.stat().st_size for path in self.folder.glob("features/*")
        )
        self.assertLessEqual(on_disk / 1e6, budget + 0.01)

    def test_age_limit_removes_old_frames(self) -> None:
        ids = self.add(10, unix_ms=0, spacing_ms=60_000)  # one frame per minute, minutes 0 to 9
        self.store.prune(max_frames=10_000, max_minutes=3.5, max_mb=1e6, now_unix_ms=9 * 60_000)
        self.assertEqual([frame["id"] for frame in self.store.recent_keyframes(50)], ids[:5:-1])

    def test_pictures_used_by_an_item_are_never_pruned_until_the_item_is_deleted(self) -> None:
        ids = self.add(10)
        item_id = self.store.create_item("Wallet", 1)
        self.store.append_frame(item_id, "ref", ids[0])
        self.store.append_frame(item_id, "crumb", ids[1])
        self.store.prune(max_frames=2, max_minutes=1e6, max_mb=1e6, now_unix_ms=1_100_000)
        left = sorted(frame["id"] for frame in self.store.recent_keyframes(50))
        self.assertEqual(left, [ids[0], ids[1], ids[8], ids[9]])
        self.assertEqual(self.store.usage()["item_frames"], 2)

        self.store.delete_item(item_id)
        self.store.prune(max_frames=2, max_minutes=1e6, max_mb=1e6, now_unix_ms=1_100_000)
        self.assertEqual(sorted(frame["id"] for frame in self.store.recent_keyframes(50)), [ids[8], ids[9]])
        self.assertEqual(self.store.items(), [])

    def test_imu_log_stays_under_its_limit(self) -> None:
        samples = [imu_sample(board_ms=index) for index in range(200)]
        for _ in range(25):
            self.store.log_imu(samples, file_minutes=0.0, max_mb=0.06)
            time.sleep(0.003)
        self.store._close_imu()
        files = list((self.folder / "imu").glob("imu-*.csv"))
        total = sum(path.stat().st_size for path in files)
        one = max(path.stat().st_size for path in files)
        self.assertGreater(one, 5_000)
        self.assertLessEqual(total, 60_000 + one)
        self.assertLess(len(files), 25)
        header = files[-1].read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(header.startswith("host_unix,board_ms,ax_g"))


if __name__ == "__main__":
    unittest.main()
