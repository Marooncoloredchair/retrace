"""Keep a public phone tunnel up after Serveo expires it.

Serveo closes an anonymous HTTP forward about 12 minutes and 5 seconds after
it opens. This script starts another forward and opens a new QR code. The
ReTrace server and the session token stay as they are. The iPhone has to scan
the new image because the hostname changes. The token is not printed.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QR_PATH = ROOT / "phone-qr-current.png"
HOST = re.compile(r"https://[A-Za-z0-9.-]+")
SSH = [
    "ssh",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ExitOnForwardFailure=yes",
    "-R",
    "80:127.0.0.1:8000",
    "serveo.net",
]


def load_make_qr():
    spec = importlib.util.spec_from_file_location("make_qr", Path(__file__).with_name("make_qr.py"))
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load scripts/make_qr.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_qr(make_qr, tunnel: str) -> Path:
    import qrcode

    url = make_qr.phone_url(tunnel, make_qr.phone_token())
    image = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    image.add_data(url)
    image.make(fit=True)
    picture = image.make_image(fill_color="black", back_color="white")
    candidates = [QR_PATH, *QR_PATH.parent.glob("phone-qr-current-*.png")]
    stamped = QR_PATH.with_name(f"phone-qr-current-{int(time.time())}.png")
    for path in [*candidates, stamped]:
        try:
            picture.save(path)
        except OSError:
            continue
        if sys.platform.startswith("win"):
            import os

            os.startfile(path)  # type: ignore[attr-defined]
        return path
    raise OSError(f"Could not write a QR image. Close {QR_PATH.name} if it is open.")


def one_tunnel(make_qr) -> None:
    process = subprocess.Popen(
        SSH,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    opened = False
    assert process.stdout is not None
    try:
        for line in process.stdout:
            match = HOST.search(line)
            if match and not opened:
                host = match.group(0)
                opened = True
                try:
                    qr_path = write_qr(make_qr, host)
                except OSError as error:
                    print(f"Phone tunnel is up, but the QR image could not be saved. {error} Host: {host}", flush=True)
                else:
                    print(f"Phone tunnel is up. Scan {qr_path.name}. Host: {host}", flush=True)
                print("Serveo closes this host after about 12 minutes. A new QR opens when it does.", flush=True)
        code = process.wait()
    except KeyboardInterrupt:
        process.terminate()
        raise
    print(f"Phone tunnel closed (ssh exit {code}). Opening another.", flush=True)


def main() -> None:
    make_qr = load_make_qr()
    print("Starting a phone tunnel to the ReTrace server on 127.0.0.1:8000.", flush=True)
    while True:
        try:
            one_tunnel(make_qr)
        except KeyboardInterrupt:
            print("Stopped.", flush=True)
            return
        time.sleep(2)


if __name__ == "__main__":
    main()
