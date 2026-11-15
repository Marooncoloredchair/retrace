"""Record for N minutes and print usable-frame rate (for before/after AE flash).

    .\\.venv\\Scripts\\python.exe tools\\record_usable_rate.py --minutes 2 --label before
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=2.0)
    parser.add_argument("--label", default="run")
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()

    import sys

    sys.path.insert(0, str(ROOT))
    from ring.config import Config
    from ring.service import build, create_app, port_free
    import uvicorn

    data = Path(tempfile.mkdtemp(prefix=f"retrace-{args.label}-"))
    config = Config(embedder_enabled=False, vlm_provider="none", port=args.port, process_fps=10.0)
    if not port_free(args.port):
        print(f"port {args.port} busy")
        return 2
    store, link, engine = build(data, config, allow_motor=False)
    app = create_app(store, link, engine, args.port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning"))
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    started = time.time()
    print(f"recording {args.label} for {args.minutes:g} min into {data}", flush=True)
    try:
        deadline = time.monotonic() + args.minutes * 60.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    ended = time.time()
    time.sleep(0.5)
    processed = engine.processed
    usable = engine.usable_count
    rejects = dict(engine.usable_rejects)
    link_status = link.status()
    with store.lock:
        rows = store.db.execute("SELECT width, height, COUNT(*) n FROM keyframes GROUP BY width, height").fetchall()
        stored = store.db.execute("SELECT COUNT(*) n FROM keyframes").fetchone()["n"]
        stored_usable = store.db.execute("SELECT COUNT(*) n FROM keyframes WHERE usable=1").fetchone()["n"]
    minutes = max((ended - started) / 60.0, 1e-6)
    report = {
        "label": args.label,
        "minutes": round(minutes, 3),
        "processed": processed,
        "usable_analysed": usable,
        "usable_per_minute_analysed": round(usable / minutes, 3),
        "stored": stored,
        "stored_usable": stored_usable,
        "usable_per_minute_stored": round(stored_usable / minutes, 3),
        "rejects": rejects,
        "camera_fps": link_status.get("camera_fps"),
        "frames_valid": link_status.get("frames_valid"),
        "frames_invalid": link_status.get("frames_invalid"),
        "frame_sizes": [dict(r) for r in rows],
    }
    out = ROOT / "reports" / f"usable_rate_{args.label}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    print("wrote", out, flush=True)
    server.should_exit = True
    thread.join(timeout=8)
    try:
        store.close()
    except Exception:
        pass
    shutil.rmtree(data, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
