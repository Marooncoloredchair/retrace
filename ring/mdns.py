"""Announce retrace.local on the LAN so the ring can find the laptop."""

from __future__ import annotations

import socket
import struct
import threading
import time


def _name() -> bytes:
    return b"\x07retrace\x05local\x00"


def announcement(ip: str, query_id: bytes = b"\x00\x00") -> bytes:
    """One A answer for retrace.local. The question is included so a query can be echoed."""
    question = _name() + struct.pack(">HH", 1, 1)
    answer = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 120, 4) + socket.inet_aton(ip)
    header = query_id + struct.pack(">HHHHH", 0x8400, 1, 1, 0, 0)
    return header + question + answer


class MdnsAdvertiser(threading.Thread):
    def __init__(self, ip: str) -> None:
        super().__init__(daemon=True, name="retrace-mdns")
        self.ip = ip
        self.error = ""
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", 5353))
            group = socket.inet_aton("224.0.0.251") + socket.inet_aton(self.ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group)
        except OSError as error:
            self.error = str(error)
            sock.close()
            return
        sock.settimeout(1.0)
        target = ("224.0.0.251", 5353)
        next_send = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_send:
                    try:
                        sock.sendto(announcement(self.ip), target)
                    except OSError:
                        pass
                    next_send = now + 10.0
                try:
                    packet, addr = sock.recvfrom(1500)
                except (TimeoutError, socket.timeout):
                    continue
                except OSError:
                    break
                if b"\x07retrace" not in packet or len(packet) < 2:
                    continue
                try:
                    sock.sendto(announcement(self.ip, packet[:2]), addr)
                    sock.sendto(announcement(self.ip, packet[:2]), target)
                except OSError:
                    pass
        finally:
            sock.close()
