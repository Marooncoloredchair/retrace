"""Parser and loop split for the standalone IMU logger dump."""

from __future__ import annotations

import struct
import unittest

from ring.pdr_import import loop_pairs, parse_dump, sample_rate_hz, slice_loop, to_rows


def _dump(samples: list[tuple], marks: list[int]) -> bytes:
    lines = [
        "RETRACE_LOG v1",
        f"samples {len(samples)}",
        f"marks {len(marks)}",
        "period_us 10000",
        "t0_us 1000",
        "accel_lsb_per_g 4096",
        "gyro_lsb_per_dps 32.8",
        "full 0",
        "dropped 0",
        "imu_fail 0",
        "END_HEADER",
    ]
    body = bytearray(("\n".join(lines) + "\n").encode("ascii"))
    for sample in samples:
        body.extend(struct.pack("<I6h", *sample))
    for mark in marks:
        body.extend(struct.pack("<I", mark))
    body.extend(b"END\n")
    return bytes(body)


class DumpParseTest(unittest.TestCase):
    def test_scales_rate_and_consecutive_pairs(self) -> None:
        samples = []
        for index in range(300):
            samples.append((index * 10000, 4096, 0, 0, 328, 0, 0))
        blob = b"logger samples=1 saved=1\n" + _dump(samples, [0, 1_000_000, 2_000_000])
        parsed = parse_dump(blob)
        self.assertEqual(len(parsed["samples"]), 300)
        self.assertEqual(parsed["marks_us"], [0, 1_000_000, 2_000_000])
        self.assertAlmostEqual(sample_rate_hz(parsed["samples"]), 100.0, places=3)
        rows = to_rows(parsed, wall0=1_000.0)
        self.assertAlmostEqual(rows[1]["ax_g"], 1.0, places=4)
        self.assertAlmostEqual(rows[1]["gx_dps"], 10.0, places=3)
        self.assertEqual(loop_pairs(parsed["marks_us"]), [(0, 1_000_000), (1_000_000, 2_000_000)])
        first = slice_loop(rows, 0, 1_000_000)
        self.assertEqual(first[0]["t_us"], 0)
        self.assertEqual(first[-1]["t_us"], 1_000_000)
        self.assertLess(first[-1]["t_us"], 2_000_000)

    def test_a_single_mark_makes_no_loop(self) -> None:
        self.assertEqual(loop_pairs([5000]), [])


if __name__ == "__main__":
    unittest.main()
