"""Wi-Fi framing on a socket, mDNS answer, and transport config."""

from __future__ import annotations

import socket
import threading
import unittest

from ring.config import Config
from ring.link import BytePort, _datagram_frame, framing, parse_status
from ring.mdns import announcement


def _text_frame(text: str) -> bytes:
    payload = text.encode()
    header = b"\xA5\x5A" + bytes([2, 0]) + len(payload).to_bytes(4, "little")
    crc = framing.crc32_ieee(payload).to_bytes(4, "little")
    return header + payload + crc


class WifiFramingTest(unittest.TestCase):
    def test_socket_carries_the_same_frames_as_serial(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        frame = _text_frame("status boot 1 reset POWERON up_ms 10 motor idle pulses 0 ignored 0 d3 LOW imu_drop 0 fifo_count 0 fifo_oflow 0 transport wifi jpeg_ms 12 quality 10 rssi -67")

        def accept() -> None:
            conn, _addr = server.accept()
            conn.sendall(frame)
            conn.close()

        threading.Thread(target=accept, daemon=True).start()
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        kind, payload = framing.FrameReader(BytePort(client)).next_frame()
        self.assertEqual(kind, framing.FRAME_TEXT)
        self.assertIn(b"transport wifi", payload)
        self.assertIn(b"jpeg_ms 12", payload)
        status = parse_status(payload.decode())
        self.assertEqual(status["jpeg_ms"], "12")
        self.assertEqual(status["rssi"], "-67")
        client.close()
        server.close()

    def test_udp_datagram_is_one_imu_frame(self) -> None:
        payload = bytes(12 + 12)
        header = b"\xA5\x5A" + bytes([4, 0]) + len(payload).to_bytes(4, "little")
        crc = framing.crc32_ieee(payload).to_bytes(4, "little")
        kind, body = _datagram_frame(framing, header + payload + crc)
        self.assertEqual(kind, framing.FRAME_IMU_BATCH)
        self.assertEqual(body, payload)
        self.assertIsNone(_datagram_frame(framing, header + payload + b"\x00\x00\x00\x00"))

    def test_mdns_answer_names_retrace_and_the_laptop(self) -> None:
        packet = announcement("192.168.43.12", b"\x12\x34")
        self.assertTrue(packet.startswith(b"\x12\x34"))
        self.assertIn(b"\x07retrace\x05local\x00", packet)
        self.assertIn(socket.inet_aton("192.168.43.12"), packet)

    def test_transport_must_be_serial_or_wifi(self) -> None:
        Config(ring_transport="wifi", ring_wifi_port=8011).validate()
        with self.assertRaises(ValueError):
            Config(ring_transport="bluetooth").validate()


if __name__ == "__main__":
    unittest.main()
