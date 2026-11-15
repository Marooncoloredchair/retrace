"""The one owner of the ring's USB serial port.

It reads camera, IMU and text frames with the unchanged reader in
firmware/xiao_sense_usb_motor/framing.py, reopens the port when the ring is
unplugged or stops sending, and is the only code that writes to the port. The
only byte it ever writes is V.

The port is opened the way the tested viewer opens it (115200, DTR and RTS
off), which does not restart the board.
"""

from __future__ import annotations

import importlib.util
import socket
import threading
import time
import zlib
from collections import deque
from pathlib import Path
from typing import Callable

from ring.config import FIRMWARE_TICK_REST_MS

ROOT = Path(__file__).resolve().parents[1]
FRAMING_PATH = ROOT / "firmware" / "xiao_sense_usb_motor" / "framing.py"
VIBRATE_COMMAND = b"V"
VIBRATE_TICK_COMMAND = b"v"
IMU_WHOAMI = 0x68


def _datagram_frame(framing, data: bytes) -> tuple[int, bytes] | None:
    """One UDP payload is one full frame: header, body, crc."""
    if len(data) < framing.HEADER.size + 4:
        return None
    magic, kind, _reserved, length = framing.HEADER.unpack_from(data)
    if magic != framing.MAGIC or kind not in (framing.FRAME_IMU, framing.FRAME_IMU_BATCH, framing.FRAME_TEXT):
        return None
    total = framing.HEADER.size + length + 4
    if length > 1200 or len(data) < total:
        return None
    payload = data[framing.HEADER.size : framing.HEADER.size + length]
    crc = int.from_bytes(data[framing.HEADER.size + length : total], "little")
    if crc != framing.crc32_ieee(payload):
        return None
    return kind, payload


def load_framing():
    spec = importlib.util.spec_from_file_location("ring_framing", FRAMING_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"The frame reader is missing: {FRAMING_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


framing = load_framing()
SLOW_CRC = framing.crc32_ieee


def fast_crc(payload: bytes) -> int:
    return zlib.crc32(payload) & 0xFFFFFFFF


# The reader's own CRC is a Python loop over every bit of every byte the ring sends, which keeps
# the interpreter busy while the rest of the service needs it. zlib computes the same CRC-32 in C.
# This replaces the function on this private copy of the module only; the file on disk and every
# other program that imports it are untouched. tests/test_ring_link.py checks the two agree.
framing.crc32_ieee = fast_crc


def open_ring_port():
    """Find the XIAO by its USB ID and open it. Raises when it is not there."""
    from app.xiao_source import find_xiao_port

    name = find_xiao_port()
    return framing.open_port(name), name


def parse_status(text: str) -> dict | None:
    """'status boot 1 reset POWERON up_ms 2345 ...' as a dict of strings."""
    tokens = text.split()
    if not tokens or tokens[0] != "status" or len(tokens) % 2 == 0:
        return None
    return dict(zip(tokens[1::2], tokens[2::2]))


def _rate(times: deque, now: float, span: float = 2.0) -> float:
    """Arrivals per second over the last two seconds, or over the time since the first one if shorter."""
    while times and now - times[0] > span:
        times.popleft()
    if len(times) < 2 or now <= times[0]:
        return 0.0
    return round((len(times) - 1) / (now - times[0]), 1)


class BytePort:
    """A TCP socket with the read/write/close shape the frame reader uses for serial."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        sock.settimeout(0.2)

    def read(self, count: int) -> bytes:
        try:
            data = self._sock.recv(count)
        except (TimeoutError, socket.timeout):
            return b""
        if not data:
            raise ConnectionError("wifi closed")
        return data

    def write(self, data: bytes) -> int:
        self._sock.sendall(data)
        return len(data)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class RingLink(threading.Thread):
    """Background reader. Other threads call take(), request_pulse() and status()."""

    def __init__(
        self,
        opener: Callable[[], tuple] = open_ring_port,
        stall_seconds: float = 5.0,
        reconnect_seconds: float = 1.0,
        allow_writes: bool = True,
        sinks: tuple = (),
        transport: str = "serial",
        wifi_port: int = 8011,
    ) -> None:
        super().__init__(daemon=True, name="ring-link")
        self.opener = opener
        self.transport = transport if transport in ("serial", "wifi") else "serial"
        self.wifi_port = int(wifi_port)
        self._listen: socket.socket | None = None
        self._mdns = None
        self.stall_seconds = stall_seconds
        self.reconnect_seconds = reconnect_seconds
        self.allow_writes = allow_writes
        self.sinks = tuple(sinks)
        self.use_tick_command = False  # True → write 'v' when firmware supports short ticks
        # The tick's 220 ms rest starts when the board reports the pulse end, not when
        # the byte was written. Hold the next 'v' until that rest is over so the board
        # does not ignore it.
        self._tick_rest_until = 0.0
        self.cond = threading.Condition()
        self._stop_flag = threading.Event()
        self._pulse_wanted = 0
        self._outbox = bytearray()
        self.connected = False
        self.port_name = ""
        self.error = "Not started"
        self.connects = 0
        self.disconnects = 0
        self.frame_seq = 0
        self.latest: dict | None = None
        self.frames_valid = 0
        self.frames_invalid = 0
        self.imu_count = 0
        self.imu_bad = 0
        self.imu_dropped = 0
        self._imu_anchor_unix: float | None = None
        self._imu_anchor_mono: float | None = None
        self._imu_last_counter: int | None = None
        self._fifo_order = ""
        self._fifo_votes = 0
        self.last_imu: dict | None = None
        self.imu_pending: list[dict] = []
        self.frame_times: deque[float] = deque()
        self.imu_times: deque[float] = deque()
        self.board: dict = {}
        self.board_restarts = 0
        self.sensor_line = ""
        self.imu_line = ""
        self.text_log: deque[str] = deque(maxlen=30)
        self.writes = 0
        self.board_pulse_starts = 0
        self.board_pulse_ends = 0
        self.board_ignored = 0
        self.last_pulse_on_us: int | None = None
        self.events: deque[dict] = deque(maxlen=50)
        self.udp_packets = 0
        self.udp_bad = 0
        self._udp: socket.socket | None = None
        self._udp_thread: threading.Thread | None = None
        self.last_data_at = 0.0
        self.latency_ms = 0
        self.rssi_dbm: int | None = None

    # ---- called from other threads -------------------------------------------------

    def stop(self) -> None:
        self._stop_flag.set()
        if self._udp is not None:
            try:
                self._udp.close()
            except OSError:
                pass
        if self._listen is not None:
            try:
                self._listen.close()
            except OSError:
                pass
        if self._mdns is not None:
            self._mdns.stop()
        with self.cond:
            self.cond.notify_all()

    def send_bytes(self, data: bytes) -> None:
        """Queue bytes for the ring. The reader thread writes them between frames."""
        with self.cond:
            self._outbox.extend(data)

    def request_pulse(self) -> bool:
        """Queue one V. False when nothing will be written."""
        with self.cond:
            if not self.allow_writes or not self.connected:
                return False
            self._pulse_wanted += 1
            return True

    def take(self, after_seq: int, timeout: float) -> dict | None:
        """The newest camera frame with a sequence above after_seq, with the IMU samples since the last take."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.latest is None or self.latest["seq"] <= after_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop_flag.is_set():
                    return None
                self.cond.wait(remaining)
            frame = dict(self.latest)
            frame["imu_samples"] = self.imu_pending
            self.imu_pending = []
            return frame

    def drain_imu(self) -> list[dict]:
        """IMU samples waiting for a camera frame. Walking must not wait on that frame."""
        with self.cond:
            samples = self.imu_pending
            self.imu_pending = []
            return samples

    def latest_jpeg(self) -> tuple[int, bytes] | None:
        with self.cond:
            if self.latest is None or not self.connected:
                return None
            return self.latest["seq"], self.latest["jpeg"]

    def drain_events(self) -> list[dict]:
        with self.cond:
            events = list(self.events)
            self.events.clear()
            return events

    def status(self) -> dict:
        now = time.monotonic()
        with self.cond:
            imu = self.last_imu
            imu_age = None if imu is None else round(now - imu["host_mono"], 2)
            imu_ok = bool(
                self.connected and imu is not None and imu_age is not None and imu_age < 1.0 and imu.get("scaled")
                and imu.get("whoami") == IMU_WHOAMI
            )
            return {
                "connected": self.connected,
                "transport": self.transport,
                "latency_ms": self.latency_ms,
                "rssi_dbm": self.rssi_dbm,
                "port": self.port_name,
                "error": self.error,
                "connects": self.connects,
                "disconnects": self.disconnects,
                "camera_fps": _rate(self.frame_times, now) if self.connected else 0.0,
                "frames_valid": self.frames_valid,
                "frames_invalid": self.frames_invalid,
                "imu": {
                    "ok": imu_ok,
                    "rate_hz": _rate(self.imu_times, now) if self.connected else 0.0,
                    "samples": self.imu_count,
                    "bad": self.imu_bad,
                    "dropped": self.imu_dropped,
                    "fifo_order": self._fifo_order,
                    "age_s": imu_age,
                    "last": None
                    if imu is None or not imu.get("scaled")
                    else {key: round(imu[key], 3) for key in ("ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps")},
                    "line": self.imu_line,
                },
                "board": dict(self.board),
                "board_restarts": self.board_restarts,
                "sensor": self.sensor_line,
                "writes": self.writes,
                "writes_allowed": self.allow_writes,
                "board_pulse_starts": self.board_pulse_starts,
                "board_pulse_ends": self.board_pulse_ends,
                "board_ignored": self.board_ignored,
                "last_pulse_on_us": self.last_pulse_on_us,
                "udp_packets": self.udp_packets,
                "udp_bad": self.udp_bad,
            }

    # ---- reader thread -------------------------------------------------------------

    def _event(self, kind: str, detail: str = "") -> None:
        self.events.append({"kind": kind, "detail": detail, "unix_ms": int(time.time() * 1000)})

    def _open_wifi(self):
        if self._listen is None:
            from ring.mdns import MdnsAdvertiser
            from ring.net import lan_ipv4

            listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listen.bind(("0.0.0.0", self.wifi_port))
            listen.listen(1)
            listen.settimeout(0.5)
            self._listen = listen
            self._mdns = MdnsAdvertiser(lan_ipv4())
            self._mdns.start()
        assert self._listen is not None
        while not self._stop_flag.is_set():
            try:
                conn, addr = self._listen.accept()
            except socket.timeout:
                continue
            except OSError as error:
                if self._stop_flag.is_set():
                    break
                raise error
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return BytePort(conn), f"wifi {addr[0]}"
        raise ConnectionError("stopped")

    def _start_udp(self) -> None:
        """Motion samples arrive here so a stalled TCP write cannot stop them."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.wifi_port + 1))
        sock.settimeout(0.5)
        self._udp = sock
        self._udp_thread = threading.Thread(target=self._udp_loop, name="ring-imu-udp", daemon=True)
        self._udp_thread.start()

    def _udp_loop(self) -> None:
        framing = load_framing()
        sock = self._udp
        assert sock is not None
        while not self._stop_flag.is_set():
            try:
                data, _addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._stop_flag.is_set():
                    return
                continue
            parsed = _datagram_frame(framing, data)
            now = time.monotonic()
            if parsed is None:
                with self.cond:
                    self.udp_bad += 1
                continue
            kind, payload = parsed
            with self.cond:
                self.udp_packets += 1
                self.last_data_at = now
            if kind in (framing.FRAME_IMU, framing.FRAME_IMU_BATCH):
                self._imu(payload, now)
            elif kind == framing.FRAME_TEXT:
                self._text(payload.decode("utf-8", errors="replace"))

    def run(self) -> None:
        if self.transport == "wifi":
            self._start_udp()
        while not self._stop_flag.is_set():
            port = None
            try:
                if self.transport == "wifi":
                    port, name = self._open_wifi()
                else:
                    port, name = self.opener()
            except Exception as error:
                with self.cond:
                    self.error = str(error)
                self._stop_flag.wait(self.reconnect_seconds)
                continue
            with self.cond:
                self.connected = True
                self.port_name = name
                self.error = ""
                self.connects += 1
                self._pulse_wanted = 0
                self.last_data_at = time.monotonic()
                self._event("connected", name)
                self.cond.notify_all()
            reason = "stopped"
            try:
                reason = self._read(port)
            except Exception as error:
                reason = str(error) or type(error).__name__
            finally:
                try:
                    port.close()
                except Exception:
                    pass
            with self.cond:
                self.connected = False
                self._pulse_wanted = 0
                self.board = {}
                if not self._stop_flag.is_set():
                    self.disconnects += 1
                    self.error = f"Ring disconnected: {reason}"
                    self._event("disconnected", reason)
                self.cond.notify_all()
            self._stop_flag.wait(self.reconnect_seconds)

    def _read(self, port) -> str:
        reader = framing.FrameReader(port)
        while not self._stop_flag.is_set():
            wanted = 0
            out = b""
            with self.cond:
                if self._pulse_wanted:
                    wanted, self._pulse_wanted = 1, 0
                if self._outbox:
                    out = bytes(self._outbox)
                    self._outbox.clear()
            if out:
                port.write(out)
            if wanted and self.allow_writes:
                with self.cond:
                    hold_tick = self.use_tick_command and time.monotonic() < self._tick_rest_until
                if hold_tick:
                    with self.cond:
                        self._pulse_wanted += 1
                else:
                    port.write(VIBRATE_TICK_COMMAND if self.use_tick_command else VIBRATE_COMMAND)
                    with self.cond:
                        self.writes += 1
            seen = reader.bytes_read
            frame = reader.next_frame()
            now = time.monotonic()
            # The logger firmware sends a text line each second, not camera frames.
            # Those bytes keep the port open. Closing it to reconnect resets the board.
            if reader.bytes_read > seen:
                self.last_data_at = now
            if frame is None:
                imu_fresh = False
                with self.cond:
                    imu = self.last_imu
                    if imu is not None and imu.get("host_mono") is not None:
                        imu_fresh = now - float(imu["host_mono"]) < 2.0
                # IMU-only mode keeps the socket. A quiet TCP stream is not a hang-up while samples are arriving.
                if not imu_fresh and now - self.last_data_at > self.stall_seconds:
                    return f"no data for {self.stall_seconds:.0f} s"
                continue
            self.last_data_at = now
            kind, payload = frame
            if kind in (framing.FRAME_IMU, framing.FRAME_IMU_BATCH):
                self._imu(payload, now)
            elif kind == framing.FRAME_JPEG:
                self._jpeg(payload, now)
            elif kind == framing.FRAME_TEXT:
                self._text(payload.decode("utf-8", errors="replace"))
        return "stopped"

    def _imu(self, payload: bytes, now: float) -> None:
        samples = framing.decode_imu_any(payload)
        if not samples:
            with self.cond:
                self.imu_bad += 1
            return
        host_unix = time.time()
        with self.cond:
            stamped = self._stamp_imu_batch(samples, host_unix, now)
            for sample in stamped:
                self.imu_count += 1
                self.last_imu = sample
                self.imu_times.append(now)
                self.imu_pending.append(sample)
            if len(self.imu_pending) > 8000:
                self.imu_dropped += len(self.imu_pending) - 8000
                del self.imu_pending[:-8000]
        for sample in stamped:
            for sink in self.sinks:
                sink.on_imu(sample)

    def _stamp_imu_batch(self, samples: list[dict], host_unix: float, host_mono: float) -> list[dict]:
        """Place each sample on the 10 ms counter grid. The newest sample in a gap lines up with arrival."""
        samples = [self._lock_fifo_order(sample) for sample in samples]
        if samples[0].get("sample_counter") is None:
            for sample in samples:
                sample["host_mono"] = host_mono
                sample["host_unix"] = host_unix
            return samples
        first = int(samples[0]["sample_counter"])
        last = int(samples[-1]["sample_counter"])
        if self._imu_last_counter is not None and last <= self._imu_last_counter:
            return []
        if self._imu_last_counter is not None and first <= self._imu_last_counter:
            skip = self._imu_last_counter - first + 1
            samples = samples[skip:]
            if not samples:
                return []
            first = int(samples[0]["sample_counter"])
            last = int(samples[-1]["sample_counter"])
        gap = bool(samples[0].get("gap"))
        if self._imu_last_counter is not None and first > self._imu_last_counter + 1:
            self.imu_dropped += first - self._imu_last_counter - 1
            gap = True
        if self._imu_anchor_unix is None or gap:
            self._imu_anchor_unix = host_unix - last * framing.IMU_PERIOD_S
            self._imu_anchor_mono = host_mono - last * framing.IMU_PERIOD_S
        for sample in samples:
            counter = int(sample["sample_counter"])
            sample["host_unix"] = self._imu_anchor_unix + counter * framing.IMU_PERIOD_S
            sample["host_mono"] = self._imu_anchor_mono + counter * framing.IMU_PERIOD_S
            sample["sample_ms"] = counter * 10
        self._imu_last_counter = last
        return samples

    def _lock_fifo_order(self, sample: dict) -> dict:
        """Keep accel,gyro unless a still sample only looks like 1 g with the words swapped."""
        if "ax_g" not in sample or sample.get("sample_counter") is None:
            return sample
        if not self._fifo_order:
            mag = (sample["ax_g"] ** 2 + sample["ay_g"] ** 2 + sample["az_g"] ** 2) ** 0.5
            fs = int(sample.get("accel_fs", 0))
            lsb = 32768 / framing.ACCEL_FULL_SCALE_G[fs if 0 <= fs <= 3 else 0]
            swapped = (sample["gx_raw"] ** 2 + sample["gy_raw"] ** 2 + sample["gz_raw"] ** 2) ** 0.5 / lsb
            if 0.75 <= mag <= 1.25 and swapped < 0.35:
                self._fifo_votes += 1
            elif 0.75 <= swapped <= 1.25 and mag < 0.35:
                self._fifo_votes -= 1
            if self._fifo_votes >= 20:
                self._fifo_order = "accel_gyro"
            elif self._fifo_votes <= -20:
                self._fifo_order = "gyro_accel"
        if self._fifo_order == "gyro_accel":
            return framing.apply_fifo_order(sample, "gyro_accel")
        return sample

    def _jpeg(self, payload: bytes, now: float) -> None:
        if len(payload) < 64 or payload[:2] != b"\xff\xd8" or payload[-2:] != b"\xff\xd9":
            with self.cond:
                self.frames_invalid += 1
            return
        unix = time.time()
        with self.cond:
            self.frame_seq += 1
            self.frames_valid += 1
            self.frame_times.append(now)
            self.latest = {
                "seq": self.frame_seq,
                "jpeg": payload,
                "host_mono": now,
                "host_unix": unix,
                "imu": self.last_imu,
                "connection": self.connects,
            }
            imu = self.last_imu
            self.cond.notify_all()
        for sink in self.sinks:
            sink.on_frame(payload, unix, now, imu)

    def _text(self, text: str) -> None:
        with self.cond:
            self.text_log.append(text)
            status = parse_status(text)
            if status is not None:
                previous = self.board
                try:
                    restarted = bool(previous) and (
                        status.get("boot") != previous.get("boot")
                        or int(status.get("up_ms", 0)) + 1500 < int(previous.get("up_ms", 0))
                    )
                except ValueError:
                    restarted = False
                if restarted:
                    self.board_restarts += 1
                    self._event("board restarted", f"reset reason {status.get('reset', '?')}")
                self.board = status
                try:
                    self.latency_ms = int(status.get("jpeg_ms") or 0)
                except ValueError:
                    pass
                try:
                    self.rssi_dbm = int(status["rssi"]) if "rssi" in status else None
                except ValueError:
                    self.rssi_dbm = None
            elif text.startswith("motor pulse ") and " start " in text:
                self.board_pulse_starts += 1
            elif text.startswith("motor pulse ") and " end on_us " in text:
                self.board_pulse_ends += 1
                try:
                    self.last_pulse_on_us = int(text.split(" on_us ")[1].split()[0])
                except (IndexError, ValueError):
                    self.last_pulse_on_us = None
                if (
                    self.use_tick_command
                    and self.last_pulse_on_us is not None
                    and self.last_pulse_on_us < 150_000
                ):
                    self._tick_rest_until = time.monotonic() + FIRMWARE_TICK_REST_MS / 1000.0
            elif text.startswith("motor command ignored"):
                self.board_ignored += 1
            elif text.startswith("sensor "):
                self.sensor_line = text
            elif text.startswith("imu "):
                self.imu_line = text
