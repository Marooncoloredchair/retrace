"""Serve the pocket guide. Does not open the ring.

    .\\retrace.cmd pocket

The iPhone must use the https address. Safari shares the compass only on a
secure page. The certificate is self-signed and is written under recordings/.
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ring.net import lan_ipv4
from ring.service import port_free

WEB = Path(__file__).resolve().parent / "web"
ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = ROOT / "recordings" / "pocket_https"
HTTP_PORT = 8765
HTTPS_PORT = 8443

FILES = {
    "/": WEB / "pocket.html",
    "/pocket.html": WEB / "pocket.html",
    "/pocket.js": WEB / "pocket.js",
}


class RingPulser:
    """Send one 'v' to the logger. Never sends 'D' or 'N'."""

    def __init__(self, port_name: str = "COM5") -> None:
        self.port_name = port_name
        self._port = None
        self._lock = threading.Lock()

    def pulse(self) -> str:
        import serial

        with self._lock:
            try:
                port = self._port
                if port is None or not port.is_open:
                    port = serial.Serial()
                    port.port = self.port_name
                    port.baudrate = 115200
                    port.timeout = 0
                    port.write_timeout = 0.5
                    port.dtr = False
                    port.rts = False
                    port.open()
                    self._port = port
                port.write(b"v")
                return ""
            except (OSError, serial.SerialException) as error:
                self._port = None
                return str(error)

    def close(self) -> None:
        with self._lock:
            if self._port is not None:
                self._port.close()
                self._port = None


PULSER = RingPulser()


class PocketHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        target = FILES.get(path)
        if target is None or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        kind = "text/html; charset=utf-8" if target.suffix == ".html" else "text/javascript; charset=utf-8"
        self._send(200, kind, body)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(min(length, 4096))
        if path != "/pulse":
            self.send_error(404)
            return
        error = PULSER.pulse()
        body = json.dumps({"ok": not error, "error": error}).encode("utf-8")
        self._send(200 if not error else 503, "application/json", body)

    def _send(self, status: int, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def _lan_ips() -> list[str]:
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            if ip not in found:
                found.append(ip)
    except OSError:
        pass
    primary = lan_ipv4()
    if primary and not primary.startswith("127.") and primary not in found:
        found.insert(0, primary)
    return found


def _der_len(length: int) -> bytes:
    if length < 128:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _der_int(value: int) -> bytes:
    if value == 0:
        body = b"\x00"
    else:
        body = value.to_bytes((value.bit_length() + 7) // 8, "big")
        if body[0] & 0x80:
            body = b"\x00" + body
    return b"\x02" + _der_len(len(body)) + body


def _der_seq(parts: list[bytes]) -> bytes:
    body = b"".join(parts)
    return b"\x30" + _der_len(len(body)) + body


def _pem(kind: str, body: bytes) -> str:
    import base64

    text = base64.encodebytes(body).decode("ascii")
    return f"-----BEGIN {kind}-----\n{text}-----END {kind}-----\n"


def _pkcs8_from_parts(parts: dict) -> bytes:
    import base64

    def num(name: str) -> int:
        return int.from_bytes(base64.b64decode(parts[name]), "big")

    pkcs1 = _der_seq([
        _der_int(0),
        _der_int(num("n")),
        _der_int(num("e")),
        _der_int(num("d")),
        _der_int(num("p")),
        _der_int(num("q")),
        _der_int(num("dp")),
        _der_int(num("dq")),
        _der_int(num("qi")),
    ])
    oid = bytes.fromhex("06092a864886f70d010101")
    algorithm = _der_seq([oid, b"\x05\x00"])
    return _der_seq([_der_int(0), algorithm, b"\x04" + _der_len(len(pkcs1)) + pkcs1])


def ensure_cert(ips: list[str]) -> tuple[Path, Path] | None:
    """Write a self-signed cert that names this machine, if Windows can."""
    import json

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    cert = CERT_DIR / "cert.pem"
    key = CERT_DIR / "key.pem"
    stamp = CERT_DIR / "ips.txt"
    wanted = ",".join(ips)
    if cert.is_file() and key.is_file() and stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == wanted:
        return cert, key
    san = "DNS=localhost&IPAddress=127.0.0.1"
    for ip in ips:
        san += f"&IPAddress={ip}"
    parts_path = CERT_DIR / "parts.json"
    script = f"""
$ErrorActionPreference = 'Stop'
$cert = New-SelfSignedCertificate -Subject 'CN=retrace-pocket' -KeyAlgorithm RSA -KeyLength 2048 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(2) -CertStoreLocation 'Cert:\\CurrentUser\\My' -TextExtension @('2.5.29.37={{text}}1.3.6.1.5.5.7.3.1','2.5.29.17={{text}}{san}')
$rsa = [System.Security.Cryptography.X509Certificates.RSACertificateExtensions]::GetRSAPrivateKey($cert)
$p = $rsa.ExportParameters($true)
function B([byte[]]$b) {{ [Convert]::ToBase64String($b) }}
$obj = [ordered]@{{
  n = B $p.Modulus; e = B $p.Exponent; d = B $p.D; p = B $p.P; q = B $p.Q
  dp = B $p.DP; dq = B $p.DQ; qi = B $p.InverseQ
  cert = [Convert]::ToBase64String($cert.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Cert))
}}
$obj | ConvertTo-Json -Compress | Set-Content -Path '{parts_path}' -Encoding ascii
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            check=True,
            capture_output=True,
            text=True,
        )
        parts = json.loads(parts_path.read_text(encoding="utf-8"))
        import base64

        cert.write_text(_pem("CERTIFICATE", base64.b64decode(parts["cert"])), encoding="ascii")
        key.write_text(_pem("PRIVATE KEY", _pkcs8_from_parts(parts)), encoding="ascii")
        parts_path.unlink(missing_ok=True)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, ssl.SSLError, json.JSONDecodeError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        print(f"HTTPS certificate was not created: {detail.strip()}", file=sys.stderr)
        return None
    stamp.write_text(wanted + "\n", encoding="utf-8")
    return cert, key


def _serve(port: int, context: ssl.SSLContext | None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), PocketHandler)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, name=f"pocket-{port}", daemon=True)
    thread.start()
    return server


def main(argv: list[str] | None = None) -> int:
    del argv
    if not port_free(HTTP_PORT):
        print(f"Port {HTTP_PORT} is already in use.", file=sys.stderr)
        return 2
    ips = _lan_ips()
    _serve(HTTP_PORT, None)
    https_note = "HTTPS did not start. The iPhone compass needs it."
    if port_free(HTTPS_PORT):
        pair = ensure_cert(ips)
        if pair is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(pair[0], pair[1])
            _serve(HTTPS_PORT, context)
            https_note = "Safari will warn that the certificate is unknown. Open it anyway, once."
    phone = ips[0] if ips else "127.0.0.1"
    print(
        "\n".join(
            [
                "",
                "ReTrace pocket guide",
                f"  On the iPhone:  https://{phone}:{HTTPS_PORT}/",
                f"  This computer:  http://127.0.0.1:{HTTP_PORT}/",
                f"  {https_note}",
                "  The ring is not opened. Beeps come from the phone.",
                "  Stop: Ctrl+C",
                "",
            ]
        ),
        flush=True,
    )
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
