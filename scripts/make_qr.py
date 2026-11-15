"""Create a QR code that opens the ReTrace phone page.

Usage:
  python scripts/make_qr.py https://YOUR-TUNNEL.trycloudflare.com

The tunnel URL is public. The QR code adds this server's phone token, so
treat the image and the printed link as secrets.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = ROOT / ".session-token"
QR_PATH = ROOT / "phone-qr.png"


def fail(message: str, code: int = 1) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def phone_token() -> str:
    if not TOKEN_PATH.exists():
        fail("No .session-token file. Start ReTrace first, then run this script again.")
    lines = [line.strip() for line in TOKEN_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 2 or not lines[0]:
        fail(".session-token is unreadable. Stop ReTrace and start it again, then make a new QR code.")
    return lines[0]


def phone_url(tunnel: str, token: str) -> str:
    parts = urlsplit(tunnel.strip())
    if parts.scheme != "https" or not parts.hostname or parts.username:
        fail(
            "Pass the https:// address printed by cloudflared, for example:\n"
            "  python scripts/make_qr.py https://YOUR-TUNNEL.trycloudflare.com",
            code=2,
        )
    if parts.path not in {"", "/"} or parts.query:
        print("Using only the host from that URL.", file=sys.stderr)
    # Keep the token in the path. A tunnel warning page can replace the query
    # string when the visitor taps Continue, which disables Start Camera.
    return urlunsplit(("https", parts.netloc, "/phone/" + quote(token, safe=""), "", ""))


def main() -> None:
    if len(sys.argv) != 2:
        fail(
            "Usage: python scripts/make_qr.py https://YOUR-TUNNEL.trycloudflare.com",
            code=2,
        )
    try:
        import qrcode
    except ImportError:
        fail("The qrcode package is missing. From the project folder run: python -m pip install -r requirements.txt")

    url = phone_url(sys.argv[1], phone_token())
    image = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    image.add_data(url)
    image.make(fit=True)
    picture = image.make_image(fill_color="black", back_color="white")
    picture.save(QR_PATH)

    print(f"Saved {QR_PATH}")
    print("Scan it with the iPhone Camera app, then open the banner in Safari.")
    print("Do not share this QR code or the link. Both contain a session token.")
    print(url)
    if sys.platform.startswith("win"):
        try:
            os.startfile(QR_PATH)  # type: ignore[attr-defined]
        except OSError as error:
            print(f"Could not open the image automatically: {error}", file=sys.stderr)


if __name__ == "__main__":
    main()
