"""Usable frames, put-down detector, keyframe bank helpers, wear-test report."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ring.config import Config
from ring.embedder import cosine_distance, farthest_point_sample
from ring.putdown import PutDownDetector
from ring.store import Store
from ring.vision import frame_usable
from ring.weartest import write_report
from tests.ring_sim import Rig, World, encode, imu_sample


class UsableScoreTest(unittest.TestCase):
    def test_rejects_dark_blurry_occlusion_motion(self) -> None:
        config = Config()
        base = {
            "brightness": 80.0,
            "blur_score": 40.0,
            "occlusion": 0.1,
        }
        self.assertEqual(frame_usable(base, imu_sample(2.0), config), (True, ""))
        self.assertEqual(frame_usable({**base, "brightness": 5.0}, None, config)[1], "too_dark")
        self.assertEqual(frame_usable({**base, "blur_score": 1.0}, None, config)[1], "too_blurry")
        self.assertEqual(frame_usable({**base, "occlusion": 0.9}, None, config)[1], "occlusion")
        self.assertEqual(frame_usable(base, imu_sample(400.0), config)[1], "motion")

    def test_engine_stores_usable_fields(self) -> None:
        world = World(5)
        rig = Rig()
        self.addCleanup(rig.close)
        state = rig.show(world.view(0), gyro_dps=2.0)
        self.assertIn("usable", state["frames"]["last"])
        row = rig.store.recent_keyframes(1)[0]
        self.assertIn(row["usable"], (0, 1))
        self.assertIsInstance(row.get("reject_reason", ""), str)


class PutDownTest(unittest.TestCase):
    def test_reach_still_withdraw(self) -> None:
        config = Config(
            putdown_reach_accel_g=0.4,
            putdown_still_accel_g=0.15,
            putdown_withdraw_accel_g=0.3,
            putdown_still_min_ms=150,
            putdown_still_max_ms=600,
            putdown_cooldown_s=0.5,
        )
        detector = PutDownDetector(config)

        def sample(ax: float, ay: float = 0.0, az: float = 0.0, gyro: float = 0.0) -> dict:
            return {
                "scaled": True,
                "ax_g": ax,
                "ay_g": ay,
                "az_g": az,
                "gx_dps": gyro,
                "gy_dps": 0.0,
                "gz_dps": 0.0,
            }

        # Rest at ~1g
        self.assertIsNone(detector.update(sample(0.0, 0.0, 1.0), 0.0, 0))
        # Reach burst
        self.assertIsNone(detector.update(sample(0.0, 0.0, 1.8), 0.05, 50))
        # Stillness ~300 ms
        for index, t in enumerate((0.1, 0.2, 0.3, 0.4)):
            self.assertIsNone(detector.update(sample(0.0, 0.0, 1.0), t, 100 + index))
        # Withdraw
        event = detector.update(sample(0.0, 0.0, 1.7), 0.45, 450)
        self.assertIsNotNone(event)
        self.assertEqual(event.source, "detected")
        self.assertGreaterEqual(event.stillness_ms, 150)

    def test_mark_putdown_pins_window(self) -> None:
        world = World(5)
        rig = Rig()
        self.addCleanup(rig.close)
        for step in range(4):
            rig.show(world.view(step))
        before = len(rig.store.recent_keyframes(50))
        result = rig.engine.mark_putdown()
        self.assertTrue(result["ok"])
        events = rig.store.putdown_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "marked")
        pinned = sum(1 for row in rig.store.recent_keyframes(50) if row.get("pinned"))
        self.assertGreaterEqual(pinned, 1)
        self.assertGreaterEqual(before, 1)


class BankHelpersTest(unittest.TestCase):
    def test_farthest_point_sample(self) -> None:
        vectors = [
            np.array([1.0, 0.0, 0.0], dtype=np.float32),
            np.array([0.99, 0.01, 0.0], dtype=np.float32),
            np.array([0.0, 1.0, 0.0], dtype=np.float32),
            np.array([0.0, 0.0, 1.0], dtype=np.float32),
        ]
        for vector in vectors:
            vector /= np.linalg.norm(vector)
        chosen = farthest_point_sample(vectors, 3)
        self.assertEqual(len(chosen), 3)
        self.assertEqual(len(set(chosen)), 3)
        self.assertLess(cosine_distance(vectors[0], vectors[1]), 0.05)


class WearReportTest(unittest.TestCase):
    def test_write_report_files(self) -> None:
        folder = Path(tempfile.mkdtemp(prefix="retrace-wear-"))
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        store = Store(folder)
        world = World(4)
        jpeg = encode(world.view(0))
        from ring.vision import analyse

        for index in range(5):
            result = analyse(jpeg, index + 1, 1_000_000 + index * 500)
            result["usable"] = index % 2 == 0
            result["reject_reason"] = "" if result["usable"] else "too_blurry"
            kid = store.add_frame(result, 1_000_000 + index * 500, {}, role="observation")
            if index % 2 == 0:
                store.mark_bank_keyframe(kid)
                store.save_embedding(kid, np.random.randn(8).astype(np.float32))
        store.add_putdown(1_000_500, "marked", 0.0, 0.0, 1_000_000, 1_002_000)
        store.add_hold(1_000_500, "marked", 1_000_000, 0.0, 0.0, 1_000_000, 1_002_000)
        store.add_hold(1_001_000, "hold", 1_000_200, 1800.0, 0.85, 1_000_000, 1_003_000)

        out = folder / "report"
        write_report(store, out, minutes=1.0, processed=10)
        self.assertTrue((out / "summary.json").is_file())
        self.assertTrue((out / "events.json").is_file())
        self.assertTrue((out / "keyframes_sheet.jpg").is_file())
        self.assertTrue((out / "export_masked").is_dir())
        summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(summary["usable_frames"], 1)
        self.assertGreaterEqual(summary["keyframes_kept"], 1)
        self.assertIn("finger_fraction", summary)
        events = json.loads((out / "events.json").read_text(encoding="utf-8"))
        self.assertEqual(events["hold_marked_count"], 1)
        self.assertEqual(events["hold_detected_count"], 1)
        self.assertTrue((out / "export").is_dir())
        store.close()


if __name__ == "__main__":
    unittest.main()
