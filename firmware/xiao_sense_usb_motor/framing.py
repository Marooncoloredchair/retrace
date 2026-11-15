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
FRAME_IMU_BATCH = 4
MAX_JPEG = 100_000
HEADER = struct.Struct("<2sBBI")
IMU_PACKET = struct.Struct("<BBBBI7h")
IMU_BATCH_HEADER = struct.Struct("<BBBBBBHI")  # address whoami accel_fs gyro_fs flags count dropped first_counter
IMU_PERIOD_S = 0.010
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


def _scale_sample(address: int, whoami: int, accel_fs: int, gyro_fs: int, ax: int, ay: int, az: int, gx: int, gy: int, gz: int) -> dict:
    sample = {
        "address": address,
        "whoami": whoami,
        "ax_raw": ax,
        "ay_raw": ay,
        "az_raw": az,
        "gx_raw": gx,
        "gy_raw": gy,
        "gz_raw": gz,
        "temp_raw": 0,
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
        }
    )
    return sample


def decode_imu_batch(payload: bytes) -> list[dict]:
    """FIFO batch. Bytes 12+ are ax,ay,az,gx,gy,gz per sample, little-endian.

    The host may swap to gyro-then-accel if a still reading shows the first
    triplet is not gravity. That lock is applied by ``apply_fifo_order``.
    """
    if len(payload) < IMU_BATCH_HEADER.size:
        return []
    address, whoami, accel_fs, gyro_fs, flags, count, dropped, first = IMU_BATCH_HEADER.unpack_from(payload)
    body = payload[IMU_BATCH_HEADER.size :]
    if count < 1 or count > 40 or len(body) != count * 12:
        return []
    samples = []
    for index in range(count):
        ax, ay, az, gx, gy, gz = struct.unpack_from("<6h", body, index * 12)
        sample = _scale_sample(address, whoami, accel_fs, gyro_fs, ax, ay, az, gx, gy, gz)
        sample["sample_counter"] = int(first) + index
        sample["gap"] = bool(flags & 1) if index == 0 else False
        sample["dropped_before"] = int(dropped) if index == 0 else 0
        sample["fifo_order"] = "accel_gyro"
        samples.append(sample)
    return samples


def apply_fifo_order(sample: dict, order: str) -> dict:
    """Relabel a batch sample if the FIFO wrote gyro before accel."""
    if order != "gyro_accel" or sample.get("fifo_order") == "gyro_accel":
        return sample
    if "gx_raw" not in sample:
        return sample
    ax, ay, az = sample["ax_raw"], sample["ay_raw"], sample["az_raw"]
    gx, gy, gz = sample["gx_raw"], sample["gy_raw"], sample["gz_raw"]
    relabeled = _scale_sample(
        sample["address"], sample["whoami"], sample.get("accel_fs", 0), sample.get("gyro_fs", 0), gx, gy, gz, ax, ay, az
    )
    relabeled["sample_counter"] = sample.get("sample_counter")
    relabeled["gap"] = sample.get("gap", False)
    relabeled["dropped_before"] = sample.get("dropped_before", 0)
    relabeled["fifo_order"] = "gyro_accel"
    return relabeled


def decode_imu_any(payload: bytes) -> list[dict]:
    """One legacy 22-byte sample, or a FIFO batch. Empty when the payload is junk."""
    if len(payload) == IMU_PACKET.size:
        sample = decode_imu(payload)
        return [sample] if sample is not None else []
    return decode_imu_batch(payload)


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
        self.bytes_read = 0

    def _fill(self, count: int) -> bool:
        while len(self.buffer) < count:
            chunk = self.port.read(max(4096, count - len(self.buffer)))
            if not chunk:
                return False
            self.bytes_read += len(chunk)
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
            if magic != MAGIC or kind not in (FRAME_JPEG, FRAME_TEXT, FRAME_IMU, FRAME_IMU_BATCH) or length > MAX_JPEG:
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
