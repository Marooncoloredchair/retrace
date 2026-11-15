"""Loop dataset, fast replay, and config writing for PDR tuning."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from ring.config import Config
from ring.pdr import PdrTracker, hop_features, world_samples
from ring.pdr_cli import _run_rows
from ring.pdr_tune import load_loops, replay_loop, search, write_setting


def _synthetic(seconds: float = 16.0, rate: float = 100.0) -> list[dict]:
    rows = []
    n = int(seconds * rate)
    for i in range(n):
        t = i / rate
        rows.append(
            {
                "host_unix": 1_000_000.0 + t,
                "ax_g": 0.35 * math.sin(2 * math.pi * 0.9 * t),
                "ay_g": 0.08 * math.sin(2 * math.pi * 0.9 * t + 0.4),
                "az_g": 1.0 + 0.45 * math.sin(2 * math.pi * 1.8 * t),
                "gx_dps": 25.0 * math.sin(2 * math.pi * 0.9 * t),
                "gy_dps": 4.0 * math.sin(2 * math.pi * 1.8 * t),
                "gz_dps": 8.0,
            }
        )
    return rows


def _replay_features(rows: list[dict], config: Config) -> PdrTracker:
    hops = hop_features(world_samples(rows, config), config)
    tracker = PdrTracker(config)
    for feats in hops:
        tracker.apply_hop(feats)
    return tracker


def _swing_feats(t: float, yaw: float, axis: tuple[float, float] = (1.0, 0.0)) -> dict:
    return {
        "t": t,
        "axis": axis,
        "confidence": 5.0,
        "yaw": yaw,
        "swing_hz": 0.9,
        "horiz_std": 0.2,
        "proj_std": 0.2,
        "step_hz": 1.8,
        "vert_std": 0.4,
        "step_pow": 1.0,
        "gyro_std": 0.01,
        "acf": 0.5,
        "band": 0.8,
        "duration": 2.0,
    }


class HeadingMethodTest(unittest.TestCase):
    def test_default_stays_swing_and_rejects_an_unknown_method(self) -> None:
        self.assertEqual(Config().pdr_heading_method, "swing")
        with self.assertRaises(ValueError):
            Config(pdr_heading_method="magnetometer").validate()

    def test_swing_reverses_after_a_four_second_turn(self) -> None:
        """Yaw steps 30° per half-second hop, 210° in all. The axis stays put
        until the net yaw change over four seconds passes the flip threshold.
        """
        hops = [_swing_feats(0.5 * (index + 1), math.radians(30.0 * index)) for index in range(8)]

        def finish(method: str) -> PdrTracker:
            tracker = PdrTracker(Config(pdr_heading_method=method))
            for feats in hops:
                tracker.apply_hop(feats)
            return tracker

        swing = finish("swing")
        gyro = finish("gyro")
        hybrid = finish("hybrid")
        # The early hops are still along the original axis, so most of the
        # path is +x. The last hop has 210° of yaw in the window and reverses.
        self.assertGreater(float(swing.pos[0]), 3.5)
        self.assertLess(abs(float(swing.pos[1])), 0.5)
        self.assertAlmostEqual(abs(swing.last_heading), math.pi, delta=0.25)
        self.assertGreater(abs(float(gyro.pos[1])), 1.0)
        self.assertGreater(abs(float(hybrid.pos[1])), 1.0)
        self.assertGreater(math.hypot(float(gyro.pos[0]), float(gyro.pos[1])), 1.0)


class FeatureReplayTest(unittest.TestCase):
    def test_cached_hops_match_the_live_tracker(self) -> None:
        rows = _synthetic()
        for smooth, window, acf in ((0.0, 2.0, 0.12), (0.45, 1.5, 0.08)):
            config = Config(
                find_backend="pdr",
                pdr_heading_smooth=smooth,
                pdr_window_s=window,
                pdr_gait_acf_min=acf,
            )
            live = _run_rows(config, rows)
            cached = _replay_features(rows, config)
            self.assertAlmostEqual(cached.steps, live.steps, places=6)
            self.assertAlmostEqual(cached.path_m, live.path_m, places=6)
            self.assertAlmostEqual(float(cached.pos[0]), float(live.pos[0]), places=6)
            self.assertAlmostEqual(float(cached.pos[1]), float(live.pos[1]), places=6)
            self.assertGreater(live.steps, 1.0)


class TuneDatasetTest(unittest.TestCase):
    def test_apply_keeps_find_backend_and_writes_the_setting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ring_config.json"
            path.write_text(json.dumps({"find_backend": "place", "port": 8010}) + "\n", encoding="utf-8")
            write_setting(
                {
                    "pdr_gait_acf_min": 0.12,
                    "pdr_gait_band_min": 0.28,
                    "pdr_gait_stop_s": 2.0,
                    "pdr_swing_confidence": 2.5,
                    "pdr_window_s": 2.0,
                    "pdr_step_length": 0.7,
                    "pdr_heading_smooth": 0.0,
                },
                path,
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["find_backend"], "place")
        self.assertEqual(saved["port"], 8010)
        self.assertEqual(saved["pdr_gait_acf_min"], 0.12)
        self.assertEqual(saved["pdr_heading_smooth"], 0)

    def test_saved_loop_replays_and_ranks_closure(self) -> None:
        rows = _synthetic(seconds=12.0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "reports" / "pdr_loop_swing_test"
            folder.mkdir(parents=True)
            imu = folder / "imu.csv"
            with imu.open("w", encoding="utf-8", newline="") as handle:
                handle.write("host_unix,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps\n")
                for row in rows:
                    handle.write(
                        f"{row['host_unix']},{row['ax_g']},{row['ay_g']},{row['az_g']},"
                        f"{row['gx_dps']},{row['gy_dps']},{row['gz_dps']}\n"
                    )
            meta = {
                "label": "swing",
                "p_unix": [rows[100]["host_unix"], rows[-100]["host_unix"]],
                "step_count": 20,
                "truth_closure_m": 0,
            }
            (folder / "loop.json").write_text(json.dumps(meta), encoding="utf-8")
            loops = load_loops(root)
            self.assertEqual(len(loops), 1)
            self.assertEqual(loops[0]["label"], "swing")
            self.assertEqual(loops[0]["step_count"], 20)
            tracker, closure = replay_loop(Config(find_backend="pdr"), loops[0])
            self.assertGreater(len(tracker.trace), 0)
            self.assertGreaterEqual(closure, 0.0)

            sit = root / "sit.csv"
            walk = root / "walk.csv"
            sit.write_text(imu.read_text(encoding="utf-8"), encoding="utf-8")
            walk.write_text(imu.read_text(encoding="utf-8"), encoding="utf-8")
            cases = [
                {"name": "sit_hand", "imu": str(sit), "max_steps": 1000, "max_path_m": 1000},
                {"name": "walk30", "imu": str(walk), "truth_steps": 0, "tolerance": 10},
            ]
            grid = {key: (values[0],) for key, values in {
                "pdr_gait_acf_min": (0.12,),
                "pdr_gait_band_min": (0.28,),
                "pdr_gait_stop_s": (2.0,),
                "pdr_swing_confidence": (2.5,),
                "pdr_window_s": (2.0,),
            }.items()}
            ranked = search(
                loops,
                cases,
                shape_grid=grid,
                scale_grid={"pdr_step_length": (0.55, 0.85), "pdr_heading_smooth": (0.0,)},
            )
        self.assertEqual(len(ranked), 2)
        self.assertLessEqual(ranked[0]["mean_closure_m"], ranked[1]["mean_closure_m"])
        self.assertEqual(len(ranked[0]["loops"]), 1)
        self.assertIn("closure_m", ranked[0]["loops"][0])


if __name__ == "__main__":
    unittest.main()
