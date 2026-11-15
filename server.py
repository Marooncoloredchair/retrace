"""Start ReTrace on this computer.

Binds to loopback only. The iPhone reaches it through a Cloudflare Quick Tunnel,
not through a LAN address. Access logs are off so session tokens in query
strings are not written to the console.
"""

import uvicorn

from app.main import app

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        ws_max_size=524288,
        access_log=False,
        proxy_headers=False,
        log_level="info",
    )
