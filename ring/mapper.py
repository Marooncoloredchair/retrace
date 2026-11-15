"""Where a mapper such as ORB-SLAM3 would plug in later. None is installed today.

A mapper is fed every valid camera frame and every IMU sample at full rate, on
the serial reader's thread, so its on_frame and on_imu must return quickly
(hand the data to its own thread or process). The service only ever asks it
for status(). Until a mapper reports reliable=True, nothing in the service
uses it, and return guidance stays on visual breadcrumbs.

To add one:
  1. Subclass Mapper and implement the four methods.
  2. Pass an instance to ring.service.build(..., mapper=YourMapper()).
  3. Report state "tracking" and reliable=True only while the mapper itself
     says it is tracking. Any other state leaves the breadcrumb logic in charge.

The offline tools stay as they are: scripts/ring_record.py,
scripts/ring_slam_check.py, scripts/ring_calibrate_camera.py and
scripts/ring_export_orbslam3.py.
"""

from __future__ import annotations

MAPPER_STATES = ("off", "starting", "tracking", "lost", "failed")


class Mapper:
    name = "none"

    def start(self, info: dict) -> None:
        """info has the picture size and the calibration file path, if one exists."""

    def on_frame(self, jpeg: bytes, host_unix: float, host_mono: float, imu: dict | None) -> None:
        """One valid camera JPEG. host times are when the laptop read it, not when it was captured."""

    def on_imu(self, sample: dict) -> None:
        """One decoded IMU sample (g and degrees per second, plus the board's millisecond stamp)."""

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": "off",
            "reliable": False,
            "detail": "No mapper is installed. Guidance uses visual breadcrumbs only.",
        }

    def stop(self) -> None:
        pass


class NoMapper(Mapper):
    pass
