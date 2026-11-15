"""Binary frames from the XIAO ESP32S3 Sense USB camera.

JPEG, diagnostic text, and IMU samples use separate frame types. The JPEG
payload is the camera's JPEG bytes and nothing else. An IMU payload is the
register sample described by IMU_PACKET, not a position.
"""

from __future__ import annotations

import struct

import serial

MAGIC = b"\xA5\x5A"
FRAME_JPEG = 1
FRAME_TEXT = 2
FRAME_IMU = 3
MAX_JPEG = 100_000
HEADER = struct.Struct("<2sBBI")
IMU_PACKET = struct.Struct("<BBBBI7h")
ACCEL_FULL_SCALE_G = (2, 4, 8, 16)
GYRO_FULL_SCALE_DPS = (250, 500, 1000, 2000)


def crc32_ieee(payload: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in payload:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1))
    return (~crc) & 0xFFFFFFFF


def decode_imu(payload: bytes) -> dict | None:
    """Unpack one IMU frame. Scaled units are g and degrees/second, not a pose."""
    if len(payload) != IMU_PACKET.size:
        return None
    address, whoami, accel_fs, gyro_fs, sample_ms, ax, ay, az, temp, gx, gy, gz = IMU_PACKET.unpack(payload)
    sample = {
        "address": address,
        "whoami": whoami,
        "sample_ms": sample_ms,
        "ax_raw": ax,
        "ay_raw": ay,
        "az_raw": az,
        "temp_raw": temp,
        "gx_raw": gx,
        "gy_raw": gy,
        "gz_raw": gz,
        "scaled": False,
    }
    if accel_fs > 3 or gyro_fs > 3:
        return sample
    accel_lsb = 32768 / ACCEL_FULL_SCALE_G[accel_fs]
    gyro_lsb = 32768 / GYRO_FULL_SCALE_DPS[gyro_fs]
    sample.update(
        {
            "scaled": True,
            "accel_fs": accel_fs,
            "gyro_fs": gyro_fs,
            "ax_g": ax / accel_lsb,
            "ay_g": ay / accel_lsb,
            "az_g": az / accel_lsb,
            "gx_dps": gx / gyro_lsb,
            "gy_dps": gy / gyro_lsb,
            "gz_dps": gz / gyro_lsb,
            "temp_c": temp / 340.0 + 36.53,
        }
    )
    return sample


def open_port(name: str) -> serial.Serial:
    port = serial.Serial()
    port.port = name
    port.baudrate = 115200
    port.timeout = 0.2
    port.write_timeout = 1
    port.dtr = False
    port.rts = False
    port.open()
    return port


class FrameReader:
    """Keep already-read bytes in order. A failed header only skips one byte."""

    def __init__(self, port: serial.Serial) -> None:
        self.port = port
        self.buffer = bytearray()

    def _fill(self, count: int) -> bool:
        while len(self.buffer) < count:
            chunk = self.port.read(max(4096, count - len(self.buffer)))
            if not chunk:
                return False
            self.buffer.extend(chunk)
        return True

    def next_frame(self) -> tuple[int, bytes] | None:
        while True:
            index = self.buffer.find(MAGIC)
            if index < 0:
                if len(self.buffer) > 1:
                    del self.buffer[:-1]
                if not self._fill(len(self.buffer) + 1):
                    return None
                continue
            if index:
                del self.buffer[:index]
            if not self._fill(HEADER.size):
                return None
            magic, kind, _reserved, length = HEADER.unpack_from(self.buffer)
            if magic != MAGIC or kind not in (FRAME_JPEG, FRAME_TEXT, FRAME_IMU) or length > MAX_JPEG:
                del self.buffer[0]
                continue
            total = HEADER.size + length + 4
            if not self._fill(total):
                return None
            payload = bytes(self.buffer[HEADER.size : HEADER.size + length])
            crc = struct.unpack_from("<I", self.buffer, HEADER.size + length)[0]
            if crc != crc32_ieee(payload):
                del self.buffer[0]
                continue
            del self.buffer[:total]
            return kind, payload
