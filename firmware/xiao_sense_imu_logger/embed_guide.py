"""Build the headers the logger serves over its own Wi-Fi.

The certificate is a local demo certificate for 192.168.4.1. It is not a
password. Both headers are gitignored.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

HERE = Path(__file__).resolve().parent
PAGE = ROOT / "ring" / "web"
CERT_DIR = ROOT / "recordings" / "ring_ap_https"


def _cert_pem() -> tuple[str, str]:
    from ring.pocket import _pem, _pkcs8_from_parts

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    parts_path = CERT_DIR / "parts.json"
    san = "DNS=retrace-ring.local&IPAddress=192.168.4.1&IPAddress=127.0.0.1"
    script = f"""
$ErrorActionPreference = 'Stop'
$cert = New-SelfSignedCertificate -Subject 'CN=retrace-ring' -KeyAlgorithm RSA -KeyLength 2048 -KeyExportPolicy Exportable -NotAfter (Get-Date).AddYears(2) -CertStoreLocation 'Cert:\\CurrentUser\\My' -TextExtension @('2.5.29.37={{text}}1.3.6.1.5.5.7.3.1','2.5.29.17={{text}}{san}')
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
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True, text=True)
    import base64

    parts = json.loads(parts_path.read_text(encoding="utf-8"))
    cert = _pem("CERTIFICATE", base64.b64decode(parts["cert"]))
    key = _pem("PRIVATE KEY", _pkcs8_from_parts(parts))
    parts_path.unlink(missing_ok=True)
    return cert, key


def _c_string(text: str) -> str:
    return 'R"RETRACE(\n' + text + ')RETRACE"'


def main() -> None:
    html = (PAGE / "pocket.html").read_text(encoding="utf-8")
    js = (PAGE / "pocket.js").read_text(encoding="utf-8")
    cert_header = HERE / "guide_cert.h"
    keep_cert = cert_header.is_file() and "BEGIN CERTIFICATE" in cert_header.read_text(encoding="utf-8")
    cert = key = ""
    if not keep_cert:
        cert, key = _cert_pem()
    if ")RETRACE" in html or ")RETRACE" in js or ")RETRACE" in cert or ")RETRACE" in key:
        raise SystemExit("page or certificate contains the embed marker")
    if not keep_cert:
        cert_header.write_text(
            "#pragma once\nstatic const char GUIDE_CERT[] =\n"
            + _c_string(cert)
            + ";\nstatic const char GUIDE_KEY[] =\n"
            + _c_string(key)
            + ";\n",
            encoding="utf-8",
        )
    (HERE / "guide_page.h").write_text(
        "#pragma once\nstatic const char GUIDE_HTML[] =\n"
        + _c_string(html)
        + ";\nstatic const char GUIDE_JS[] =\n"
        + _c_string(js)
        + ";\n",
        encoding="utf-8",
    )
    print("wrote guide headers")


if __name__ == "__main__":
    main()
