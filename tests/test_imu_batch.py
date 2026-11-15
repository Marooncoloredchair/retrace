"""Host decode of the legacy 22-byte IMU frame and the FIFO batch frame."""

from __future__ import annotations

import struct
import unittest

from ring.link import framing


def _batch(samples: list[tuple[int, int, int, int, int, int]], *, fs_a: int = 2, fs_g: int = 2, first: int = 0, gap: int = 0) -> bytes:
    header = framing.IMU_BATCH_HEADER.pack(0x68, 0x68, fs_a, fs_g, gap, len(samples), 0, first)
    body = b"".join(struct.pack("<6h", *sample) for sample in samples)
    return header + body


class ImuDecodeTest(unittest.TestCase):
    def test_legacy_packet_still_scales_at_2g(self) -> None:
        payload = framing.IMU_PACKET.pack(0x68, 0x68, 0, 0, 1234, 0, 0, 16384, 0, 0, 0, 0)
        sample = framing.decode_imu(payload)
        self.assertIsNotNone(sample)
        self.assertAlmostEqual(sample["az_g"], 1.0, delta=0.01)
        self.assertEqual(framing.decode_imu_any(payload)[0]["az_g"], sample["az_g"])

    def test_batch_scales_8g_and_1000dps(self) -> None:
        # ±8 g → 4096 counts per g. ±1000 deg/s → 32.768 counts per deg/s.
        payload = _batch([(0, 0, 4096, 0, 0, 3277)], first=10)
        sample = framing.decode_imu_batch(payload)[0]
        self.assertAlmostEqual(sample["az_g"], 1.0, delta=0.01)
        self.assertAlmostEqual(sample["gz_dps"], 100.0, delta=0.2)
        self.assertEqual(sample["sample_counter"], 10)
        self.assertEqual(sample["accel_fs"], 2)
        self.assertEqual(sample["gyro_fs"], 2)

    def test_counter_steps_inside_one_batch(self) -> None:
        payload = _batch([(0, 0, 4096, 0, 0, 0), (0, 0, 4096, 0, 0, 0)], first=5, gap=1)
        samples = framing.decode_imu_batch(payload)
        self.assertEqual([row["sample_counter"] for row in samples], [5, 6])
        self.assertTrue(samples[0]["gap"])
        self.assertFalse(samples[1]["gap"])

    def test_gyro_then_accel_order_puts_gravity_back_on_accel(self) -> None:
        # Words arrive gyro (near 0) then accel (1 g on Z). Default labeling looks nothing like gravity.
        payload = _batch([(0, 0, 0, 0, 0, 4096)])
        sample = framing.decode_imu_batch(payload)[0]
        self.assertLess(abs(sample["az_g"]), 0.05)
        fixed = framing.apply_fifo_order(sample, "gyro_accel")
        self.assertAlmostEqual(fixed["az_g"], 1.0, delta=0.01)
        self.assertEqual(fixed["fifo_order"], "gyro_accel")
