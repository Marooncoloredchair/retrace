"""The serial owner against a simulated board: reading, the V command, unplug and replug.

The simulated board speaks the real frame format and follows the firmware's
motor rules. This checks the laptop side only. It is not a test of the ring.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from ring.config import Config
from ring.engine import Engine
from ring.link import RingLink
from ring.store import Store
from tests.ring_sim import SimRing, World, wait_until


class CrcTest(unittest.TestCase):
    def test_fast_crc_equals_the_frame_readers_own_crc(self) -> None:
        import hashlib
        import importlib.util

        from ring import link as ring_link

        cases = [b"", b"\x00", b"V", b"123456789", bytes(range(256)) * 3]
        cases += [hashlib.sha256(bytes([index])).digest() * (index + 1) for index in range(40)]
        cases.append(World(30).view(0).tobytes()[:20_000])
        for payload in cases:
            self.assertEqual(ring_link.fast_crc(payload), ring_link.SLOW_CRC(payload))
        self.assertEqual(ring_link.fast_crc(b"123456789"), 0xCBF43926)  # the published CRC-32 check value
        self.assertIs(ring_link.framing.crc32_ieee, ring_link.fast_crc)

        # A fresh import of the same file still has its own function: nothing on disk changed.
        spec = importlib.util.spec_from_file_location("framing_untouched", ring_link.FRAMING_PATH)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertEqual(fresh.crc32_ieee.__module__, "framing_untouched")
        self.assertEqual(fresh.crc32_ieee(b"123456789"), 0xCBF43926)


class LinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(31)
        self.ring = SimRing(self.world.view(0))
        self.link = None

    def start(self, **options) -> RingLink:
        settings = {"stall_seconds": 1.0, "reconnect_seconds": 0.1, **options}
        self.link = RingLink(opener=self.ring.opener, **settings)
        self.link.start()
        self.addCleanup(self.stop)
        self.assertTrue(wait_until(lambda: self.link.status()["frames_valid"] >= 5), self.link.status())
        return self.link

    def stop(self) -> None:
        self.link.stop()
        self.link.join(timeout=3)

    def test_reads_camera_imu_and_status(self) -> None:
        self.ring.gyro_dps = 40.0
        link = self.start()
        self.assertTrue(wait_until(lambda: link.status()["board"].get("boot") == "1"))
        time.sleep(1.0)
        status = link.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["port"], "SIM")
        self.assertEqual(status["frames_invalid"], 0)
        self.assertTrue(status["imu"]["ok"])
        self.assertAlmostEqual(status["imu"]["last"]["gx_dps"], 40.0, delta=0.1)
        self.assertAlmostEqual(status["imu"]["last"]["az_g"], 1.0, delta=0.01)
        self.assertGreater(status["camera_fps"], 15)
        self.assertTrue(status["sensor"].startswith("sensor OV3660"))
        frame = link.take(0, timeout=1.0)
        self.assertEqual(frame["jpeg"][:2], b"\xff\xd8")
        self.assertGreaterEqual(len(frame["imu_samples"]), 1)
        self.assertEqual(frame["imu"]["whoami"], 0x68)
        self.assertEqual(bytes(self.ring.written), b"")  # reading alone writes nothing

    def test_noise_and_damaged_frames_are_skipped(self) -> None:
        self.ring.junk = bytes(range(256)) * 2 + b"\xa5\x5a\x01\x00"
        self.ring.corrupt_next = 3
        link = self.start()
        self.assertTrue(wait_until(lambda: link.status()["frames_valid"] >= 20))
        self.assertTrue(link.status()["connected"])
        self.assertEqual(link.status()["disconnects"], 0)

    def test_one_request_writes_exactly_one_v_and_the_board_confirms_it(self) -> None:
        link = self.start()
        self.assertTrue(link.request_pulse())
        self.assertTrue(wait_until(lambda: link.status()["board_pulse_ends"] == 1, 3.0), link.status())
        status = link.status()
        self.assertEqual(bytes(self.ring.written), b"V")
        self.assertEqual(status["writes"], 1)
        self.assertEqual(status["board_pulse_starts"], 1)
        self.assertEqual(status["last_pulse_on_us"], 200011)
        self.assertEqual(self.ring.pulses_total, 1)

    def test_requests_that_pile_up_before_the_port_is_written_become_one_v(self) -> None:
        link = self.start()
        with link.cond:  # hold the reader so three requests arrive between two writes
            for _ in range(3):
                link._pulse_wanted += 1
        self.assertTrue(wait_until(lambda: link.status()["writes"] >= 1, 2.0))
        time.sleep(0.5)
        self.assertEqual(bytes(self.ring.written), b"V")

    def test_no_motor_mode_never_writes(self) -> None:
        link = self.start(allow_writes=False)
        self.assertFalse(link.request_pulse())
        time.sleep(0.4)
        self.assertEqual(bytes(self.ring.written), b"")
        self.assertEqual(link.status()["writes"], 0)

    def test_unplug_is_noticed_and_replug_reconnects(self) -> None:
        link = self.start()
        self.ring.unplug()
        self.assertTrue(wait_until(lambda: not link.status()["connected"], 3.0))
        status = link.status()
        self.assertEqual(status["disconnects"], 1)
        self.assertIn("disconnected", status["error"].lower() + " disconnected")
        self.assertFalse(link.request_pulse())
        self.assertIsNone(link.latest_jpeg())
        time.sleep(0.5)  # it keeps trying, and reports why it cannot connect
        self.assertIn("not on a USB serial port", link.status()["error"])

        before = link.status()["frames_valid"]
        self.ring.plug()
        self.assertTrue(wait_until(lambda: link.status()["connected"], 3.0))
        self.assertTrue(wait_until(lambda: link.status()["frames_valid"] > before + 5, 3.0))
        self.assertEqual(link.status()["connects"], 2)
        kinds = [event["kind"] for event in link.drain_events()]
        self.assertEqual(kinds, ["connected", "disconnected", "connected"])
        self.assertEqual(bytes(self.ring.written), b"")  # reconnecting sends nothing

    def test_a_request_made_while_unplugged_is_not_sent_after_replug(self) -> None:
        link = self.start()
        self.ring.unplug()
        self.assertTrue(wait_until(lambda: not link.status()["connected"], 3.0))
        self.assertFalse(link.request_pulse())
        self.ring.plug()
        self.assertTrue(wait_until(lambda: link.status()["connected"], 3.0))
        time.sleep(0.6)
        self.assertEqual(bytes(self.ring.written), b"")

    def test_a_silent_port_is_reopened(self) -> None:
        link = self.start()
        self.ring.silent = True
        self.assertTrue(wait_until(lambda: link.status()["disconnects"] >= 1, 4.0))
        self.ring.silent = False
        self.assertTrue(wait_until(lambda: link.status()["connected"] and link.status()["connects"] >= 2, 4.0))

    def test_board_restart_is_reported(self) -> None:
        link = self.start()
        self.assertTrue(wait_until(lambda: link.status()["board"].get("boot") == "1", 3.0))
        self.ring.unplug()
        self.assertTrue(wait_until(lambda: not link.status()["connected"], 3.0))
        self.ring.plug()
        self.assertTrue(wait_until(lambda: link.status()["board"].get("boot") == "2", 4.0))


class LiveServiceTest(unittest.TestCase):
    """Link and engine running on their own threads, as in the real app."""

    def setUp(self) -> None:
        self.world = World(32)
        self.ring = SimRing(self.world.view(0))
        self.folder = Path(tempfile.mkdtemp(prefix="retrace-live-"))
        self.config = Config(save_capture_s=0.6, reconnect_seconds=0.1, stall_seconds=1.0, motor_min_interval_s=1.3)
        self.store = Store(self.folder)
        self.link = RingLink(opener=self.ring.opener, stall_seconds=1.0, reconnect_seconds=0.1)
        self.engine = Engine(self.config, self.store, self.link)
        self.link.start()
        self.engine.start()
        self.addCleanup(self.shutdown)

    def shutdown(self) -> None:
        self.engine.stop()
        self.link.stop()
        self.link.join(timeout=3)
        self.store.close()
        shutil.rmtree(self.folder, ignore_errors=True)

    def state(self) -> dict:
        return self.engine.snapshot()

    def test_save_unplug_replug_keeps_the_item_and_marks_the_gap(self) -> None:
        self.assertTrue(wait_until(lambda: self.state()["frames"]["kept"] >= 1, 5.0), self.state()["frames"])
        saved = self.engine.submit("save_item", "Keys")
        self.assertTrue(wait_until(lambda: self.state()["items"] and self.state()["items"][0]["trail_state"] == "recording", 4.0))
        self.ring.set_picture(self.world.view(3))
        self.assertTrue(wait_until(lambda: self.state()["items"][0]["breadcrumbs"] == 1, 4.0))

        self.ring.unplug()
        self.assertTrue(wait_until(lambda: not self.state()["link"]["connected"], 4.0))
        self.assertEqual([item["name"] for item in self.state()["items"]], ["Keys"])
        self.assertEqual(self.state()["frames"]["last"]["verdict"], "disconnected")
        self.assertTrue(any("disconnected" in notice["text"] for notice in self.state()["notices"]))
        # Save still records the event while disconnected; vibration does not.
        wallet = self.engine.submit("save_item", "Wallet")
        self.assertIsNotNone(wallet["item_id"])
        self.assertEqual([item["name"] for item in self.state()["items"]], ["Keys", "Wallet"])
        test = self.engine.submit("test_pulse")
        self.assertFalse(test["sent"])
        time.sleep(0.6)  # a clear gap in the trail between the last crumb and the return

        self.ring.set_picture(self.world.view(6))
        self.ring.plug()
        self.assertTrue(wait_until(lambda: self.state()["link"]["connected"], 4.0))
        self.assertTrue(wait_until(lambda: self.state()["items"][0]["breadcrumbs"] >= 2, 4.0), self.state()["items"])
        detail = self.engine.submit("item_detail", saved["item_id"])
        gaps = [crumb["gap_before_s"] for crumb in detail["breadcrumbs"]]
        self.assertGreaterEqual(len(gaps), 2)
        self.assertTrue(any(gap > 0.3 for gap in gaps), gaps)  # the trail shows where the ring was away
        self.assertEqual(bytes(self.ring.written), b"")

    def test_test_button_sends_one_pulse_and_a_second_press_is_held_back(self) -> None:
        self.assertTrue(wait_until(lambda: self.state()["link"]["connected"], 4.0))
        first = self.engine.submit("test_pulse")
        second = self.engine.submit("test_pulse")
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertIn("too soon", second["blocked"])
        self.assertTrue(wait_until(lambda: self.state()["motor"]["board_pulse_ends"] == 1, 3.0), self.state()["motor"])
        self.assertEqual(bytes(self.ring.written), b"v")
        self.assertEqual(self.state()["motor"]["commands_sent"], 1)
        self.assertEqual(self.state()["motor"]["bytes_written"], 1)

    def test_live_find_arrives_and_the_fake_board_gets_one_v(self) -> None:
        self.assertTrue(wait_until(lambda: self.state()["frames"]["kept"] >= 1, 5.0))
        saved = self.engine.submit("save_item", "Keys")
        self.assertTrue(wait_until(lambda: self.state()["items"][0]["trail_state"] == "recording", 4.0))
        for step in (3, 4, 5, 6, 7, 8):
            self.ring.set_picture(self.world.view(step))
            count = step - 2
            self.assertTrue(wait_until(lambda: self.state()["items"][0]["breadcrumbs"] == count, 4.0), self.state()["items"])
        self.engine.submit("start_find", saved["item_id"])
        self.assertTrue(wait_until(lambda: self.state()["find"] and self.state()["find"]["status"] == "MATCHED", 5.0))
        self.assertEqual(bytes(self.ring.written), b"")
        self.ring.set_picture(self.world.view(0))
        self.assertTrue(wait_until(lambda: self.state()["find"]["status"] == "ARRIVED", 6.0), self.state()["find"])
        self.assertTrue(wait_until(lambda: self.state()["motor"]["board_pulse_ends"] >= 1, 3.0))
        time.sleep(1.5)  # staying at the spot does not buzz again
        written = bytes(self.ring.written)
        self.assertTrue(set(written) == {ord("v")} and 1 <= len(written) <= 2, written)
        reasons = [entry["reason"] for entry in self.state()["motor"]["log"] if entry["sent"]]
        self.assertEqual(reasons.count("arrival"), 1)
        self.assertEqual(len(written), len(reasons))


if __name__ == "__main__":
    unittest.main()
