"""Laptop address on the current network, and the phone-page token."""

from __future__ import annotations

import secrets
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = ROOT / ".ring-remote-token"


HOTSPOT_IP = "192.168.137.1"


def hotspot_up() -> bool:
    """True when the laptop hotspot address is assigned on this computer.

    The ring firmware connects to 192.168.137.1. A 5 GHz campus network is not a substitute.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((HOTSPOT_IP, 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def hotspot_warning(up: bool | None = None) -> str:
    """Loud startup warning when the hotspot is down. Empty when it is up.

    The text names the address and the script that starts the hotspot. It does not
    include the passphrase.
    """
    if up is None:
        up = hotspot_up()
    if up:
        return ""
    return (
        "!! HOTSPOT IS OFF !!\n"
        "The laptop hotspot is not up on 192.168.137.1.\n"
        "The ring connects to 192.168.137.1:8011. "
        "Princeton puvisitor (10.28.46.136) is 5 GHz and the ESP32 cannot use it.\n"
        "Start the hotspot with: powershell -ExecutionPolicy Bypass -File tools\\start_laptop_hotspot.ps1\n"
    )


def lan_ipv4() -> str:
    """Address the ring and the phone use to reach the laptop.

    The Windows mobile hotspot is 192.168.137.1. Prefer it when it is up,
    because the default route may be a 5 GHz network the ring cannot join.
    """
    if hotspot_up():
        return HOTSPOT_IP
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] == HOTSPOT_IP:
                return HOTSPOT_IP
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def ensure_dashboard_token() -> str:
    """Short token kept in .ring-remote-token, which git ignores.

    This is separate from .session-token, which belongs to the phone-camera server.
    """
    if TOKEN_PATH.is_file():
        existing = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if len(existing) == 6 and all(c in "0123456789abcdef" for c in existing):
            return existing
    token = secrets.token_hex(3)
    TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    return token
