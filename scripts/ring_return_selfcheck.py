"""Check ring_return_test.py against a fake board. Opens no serial port.

    .\\.venv\\Scripts\\python.exe scripts\\ring_return_selfcheck.py

The fake board sends the same frames the real firmware sends: an IMU frame and
a JPEG about 28 times a second, and a status line every 2 seconds. The camera
"sees" one drawn scene, then a different one, then the first again. The check
passes only if the away frames stay UNKNOWN, the return is recognized, and the
frame counts in the log match what the fake board sent.

This proves the software path. It says nothing about the real camera.
"""

from __future__ import annotations

import csv
import struct
import sys
import tempfile
import time
import zlib
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ring_return_test as rrt  # noqa: E402

framing = rrt.framing

FRAME_PERIOD_S = 0.036
ELSEWHERE_FROM_S = 3.0
BACK_IN_VIEW_FROM_S = 8.2
SCRIPT = "save@1.0,save@1.6,away@2.5,back@8.0,end@11.5"


def wire(kind: int, payload: bytes, corrupt_crc: bool = False) -> bytes:
    crc = zlib.crc32(payload) ^ (0xFFFFFFFF if corrupt_crc else 0)
    return framing.MAGIC + bytes([kind, 0]) + struct.pack("<I", len(payload)) + payload + struct.pack("<I", crc)


def scene(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((320, 420, 3), 110, dtype=np.uint8)
    for _ in range(90):
        x, y = int(rng.integers(0, 400)), int(rng.integers(0, 300))
        w, h = int(rng.integers(8, 60)), int(rng.integers(8, 50))
        color = tuple(int(value) for value in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            cv2.rectangle(image, (x, y), (x + w, y + h), color, -1 if rng.random() < 0.6 else 2)
        else:
            cv2.circle(image, (x, y), max(4, w // 3), color, -1 if rng.random() < 0.6 else 2)
    for index in range(12):
        position = (int(rng.integers(0, 330)), int(rng.integers(20, 300)))
        cv2.putText(image, f"R{seed}{index}", position, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2)
    return image


class FakeBoard:
    """A port-like object with read() only, producing frames in real time."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.next_frame = 0.0
        self.next_status = 0.0
        self.sent_jpeg = 0
        self.sent_bad_jpeg = 0
        self.sent_bad_crc = 0
        self.sent_imu = 0
        self.pending = bytearray(b"\x11\x22\x33" * 40)  # joining mid-frame: bytes with no header
        self.junk = len(self.pending)
        self.rng = np.random.default_rng(7)
        self.location = scene(1)
        self.elsewhere = scene(2)

    def _jpeg(self, elapsed: float) -> bytes:
        source = self.elsewhere if ELSEWHERE_FROM_S <= elapsed < BACK_IN_VIEW_FROM_S else self.location
        dx, dy = (int(value) for value in self.rng.integers(-12, 13, 2))
        view = source[40 + dy : 280 + dy, 50 + dx : 370 + dx].astype(np.int16)
        view += self.rng.normal(0, 3, view.shape).astype(np.int16)
        ok, encoded = cv2.imencode(".jpg", np.clip(view, 0, 255).astype(np.uint8), [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        assert ok
        return encoded.tobytes()

    def _produce(self) -> None:
        elapsed = time.monotonic() - self.started
        while self.next_status <= elapsed:
            text = f"status boot 1 reset POWERON up_ms {int(self.next_status * 1000) + 5000} motor idle pulses 0 ignored 0 d3 LOW"
            self.pending += wire(framing.FRAME_TEXT, text.encode())
            self.next_status += 2.0
        while self.next_frame <= elapsed:
            at = self.next_frame
            moving = ELSEWHERE_FROM_S - 0.5 <= at < BACK_IN_VIEW_FROM_S
            gyro = int(self.rng.normal(0, 4000 if moving else 60))
            imu = framing.IMU_PACKET.pack(0x68, 0x68, 0, 0, int(at * 1000) + 5000, 300, -200, 16200, 100, gyro, 40, -30)
            self.pending += wire(framing.FRAME_IMU, imu)
            self.sent_imu += 1
            number = self.sent_jpeg + self.sent_bad_jpeg + self.sent_bad_crc
            if number in (50, 51):  # a correctly framed payload that is not a JPEG
                self.pending += wire(framing.FRAME_JPEG, b"not a jpeg at all " * 20)
                self.sent_bad_jpeg += 1
            elif number == 80:  # a frame damaged in transit
                damaged = wire(framing.FRAME_JPEG, self._jpeg(at), corrupt_crc=True)
                self.pending += damaged
                self.junk += len(damaged)
                self.sent_bad_crc += 1
            else:
                self.pending += wire(framing.FRAME_JPEG, self._jpeg(at))
                self.sent_jpeg += 1
            self.next_frame += FRAME_PERIOD_S

    def read(self, count: int) -> bytes:
        self._produce()
        if not self.pending:
            time.sleep(0.01)
            self._produce()
        data = bytes(self.pending[:count])
        del self.pending[: len(data)]
        return data

    def close(self) -> None:
        pass


def main() -> int:
    sample = b"retrace"
    assert zlib.crc32(sample) == framing.crc32_ieee(sample), "CRC used by the fake board differs from framing.py"
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if passed else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
        if not passed:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="retrace-ring-selfcheck-") as folder:
        out_dir = Path(folder)
        board = FakeBoard()
        feed = rrt.Feed(board, out_dir / "imu.csv")
        feed.started = board.started
        test = rrt.RingTest(feed, out_dir, "FAKE", None, save_images=True)
        feed.start()
        text = rrt.run_headless(test, rrt.parse_script(SCRIPT))
        print(text)
        counts = feed.counts()
        with (out_dir / "frames.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        with (out_dir / "imu.csv").open(encoding="utf-8", newline="") as handle:
            imu_rows = list(csv.DictReader(handle))
        with (out_dir / "events.csv").open(encoding="utf-8", newline="") as handle:
            events = [row["event"] for row in csv.DictReader(handle)]
        away = [row for row in rows if row["phase"] == "away"]
        back = [row for row in rows if row["phase"] == "returned"]
        leaving = [row for row in rows if row["phase"] == "leaving"]
        slack = 3  # frames the fake board had queued when the reader stopped

        print("Self-check against the fake board:")
        check("two references saved", len(test.references) == 2 and events.count("REFERENCE_SAVED") == 2)
        check("reference photos written", len(list((out_dir / "images").glob("ref-*.jpg"))) == 2)
        check("valid camera frames match what was sent",
              0 <= board.sent_jpeg - counts["jpeg_valid"] <= slack, f"sent {board.sent_jpeg}, counted {counts['jpeg_valid']}")
        check("non-JPEG payloads counted as invalid", counts["jpeg_invalid"] == board.sent_bad_jpeg == 2,
              f"counted {counts['jpeg_invalid']}")
        check("IMU samples match what was sent",
              0 <= board.sent_imu - counts["imu_valid"] <= slack and counts["imu_bad"] == 0,
              f"sent {board.sent_imu}, counted {counts['imu_valid']}")
        check("every IMU sample is in imu.csv", len(imu_rows) == counts["imu_valid"], f"{len(imu_rows)} rows")
        check("bytes outside valid frames match the junk and the damaged frame",
              counts["unframed_total"] == board.junk, f"junk {board.junk}, counted {counts['unframed_total']}")
        check("only the joining junk is counted in the first 2 s", counts["unframed_first_2s"] == 120,
              f"counted {counts['unframed_first_2s']}")
        check("analysed at no more than 10 frames a second", 60 <= len(rows) <= 10 * 11.5 + 2, f"{len(rows)} rows")
        check("leaving frames are not scored", bool(leaving) and all(row["scored"] == "0" for row in leaving), f"{len(leaving)} rows")
        check("away frames scored", len(away) >= 15, f"{len(away)} rows")
        check("away frames all UNKNOWN", all(row["predicted"] == "UNKNOWN" and row["outcome"] == "unknown" for row in away))
        check("no confirmed frame while away", all(row["stable_predicted"] != rrt.CONFIRMED for row in away))
        named = [row for row in back if row["predicted"] == rrt.PLACE]
        check("return frames scored", len(back) >= 20, f"{len(back)} rows")
        check("return recognized on most frames", len(named) >= 0.8 * len(back), f"{len(named)} of {len(back)}")
        check("recognized frames scored correct", all(row["outcome"] == "correct" for row in named))
        check("return confirmed", any(row["stable_predicted"] == rrt.CONFIRMED and row["stable_outcome"] == "correct" for row in back))
        check("first return frames UNKNOWN before the scene is back in view",
              back[0]["predicted"] == "UNKNOWN" and back[0]["outcome"] == "unknown")
        check("each scored row carries an IMU reading", all(row["accel_mag_g"] and row["imu_board_ms"] for row in away + back))
        check("gyro readings larger while away",
              counts["phase_imu"]["away"]["gyro_max"] > 5 * counts["phase_imu"]["setup"]["gyro_max"])
        check("no board restart reported", counts["restarts"] == 0 and "BOARD_RESTART" not in events)
        check("summary names the result", "RESULT for trip 1: RECOGNIZED on return (confirmed)." in text)
        check("events in order", [e for e in events if e in {"WALKING_AWAY", "BACK_AT_LOCATION", "END_TEST"}]
              == ["WALKING_AWAY", "BACK_AT_LOCATION", "END_TEST"])
        check("the feed object has no write method", not hasattr(feed.port, "write"))

    print()
    if failures:
        print(f"SELF-CHECK FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("Self-check passed. This used a fake board and drawn scenes, not the ring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
