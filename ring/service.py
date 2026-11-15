"""The ReTrace ring service: one process that owns the ring and serves the dashboard.

Start it with retrace.cmd, or:
    .\\.venv\\Scripts\\python.exe retrace_ring.py

The dashboard listens on this computer. Requests that change state must come
from the dashboard page itself, so another web page cannot press Save, Find,
or the vibration test. The phone's Sensor Logger push and the /body fallback
page are the exception: they are reachable on the local network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sqlite3
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import serial
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ring import link as ring_link
from ring.config import Config, load_config
from ring.engine import Engine, UserError
from ring.link import RingLink
from ring.mapper import Mapper
from ring.store import Store

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = Path(__file__).resolve().parent / "web"
DATA_DIR = ROOT / "recordings" / "ring_memory"
CONFIG_PATH = ROOT / "ring_config.json"

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def open_port_friendly():
    try:
        return ring_link.open_ring_port()
    except serial.SerialException as error:
        raise RuntimeError(
            "The ring's USB port is in use by another program. Close any other ReTrace window, "
            f"camera viewer or serial monitor. ({error})"
        ) from None


class SaveBody(BaseModel):
    name: str


class EnabledBody(BaseModel):
    enabled: bool


class PdrStartBody(BaseModel):
    kind: str


class PdrEndBody(BaseModel):
    meters: float | None = None


def build(
    data_dir: Path = DATA_DIR,
    config: Config | None = None,
    opener=open_port_friendly,
    allow_motor: bool = True,
    mapper: Mapper | None = None,
    echo=None,
):
    """Create the store, the serial owner and the engine. Nothing is started yet."""
    config = config or Config()
    store = Store(data_dir)
    sinks = (mapper,) if mapper is not None else ()
    link = RingLink(
        opener=opener,
        stall_seconds=config.stall_seconds,
        reconnect_seconds=config.reconnect_seconds,
        allow_writes=allow_motor,
        sinks=sinks,
        transport=config.ring_transport,
        wifi_port=config.ring_wifi_port,
    )
    engine = Engine(config, store, link, mapper=mapper, echo=echo)
    return store, link, engine


def terminal_line(kind: str, text: str) -> None:
    """Every line of the dashboard's activity list, in the window ReTrace was started from."""
    mark = "!!" if kind == "error" else "  "
    print(f"{time.strftime('%H:%M:%S')} {mark} {text}", flush=True)


def bind_host(config: Config) -> str:
    # The phone posts sensor data to this machine. The guard still refuses
    # every other path unless the peer is this computer.
    del config
    return "0.0.0.0"


def push_ipv4() -> str:
    """The address of the network the laptop is using, not the ring's own hotspot name."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def remote_url(port: int) -> str:
    from ring.net import ensure_dashboard_token, lan_ipv4

    return f"http://{lan_ipv4()}:{port}/remote?token={ensure_dashboard_token()}"


def create_app(store: Store, link: RingLink, engine: Engine, port: int) -> FastAPI:
    from ring.net import ensure_dashboard_token

    lan = bool(engine.config.dashboard_lan)
    token = ensure_dashboard_token() if lan else ""
    local_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    hosts = set(local_hosts)
    origins = {f"http://{host}" for host in hosts}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine.mapper.start({"data_dir": str(store.root)})
        link.start()
        engine.start()
        try:
            yield
        finally:
            engine.stop()
            link.stop()
            link.join(timeout=3)
            engine.mapper.stop()
            store.close()

    app = FastAPI(title="ReTrace ring", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def refuse(text: str) -> JSONResponse:
        return JSONResponse({"detail": text}, status_code=403, headers=SECURITY_HEADERS)

    def presented_token(request: Request) -> str:
        header = request.headers.get("x-retrace-token", "")
        if header:
            return header
        return request.query_params.get("token", "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        from ring.body_track import phone_push_open

        if phone_push_open(request.url.path, request.method):
            response = await call_next(request)
            for key, value in SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)
            return response
        peer = "" if request.client is None else request.client.host
        peer_local = peer in ("127.0.0.1", "::1", "localhost")
        host = request.headers.get("host", "").lower()
        on_lan = lan and not peer_local and host not in local_hosts and presented_token(request) == token
        if not peer_local and not on_lan:
            return refuse("ReTrace only answers on this computer.")
        if host not in local_hosts and not on_lan:
            return refuse("ReTrace only answers on this computer.")
        if on_lan and request.method not in ("GET", "HEAD") and request.headers.get("x-retrace") != "1":
            return refuse("That request did not come from the ReTrace dashboard.")
        if host in local_hosts and request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                return refuse("That request did not come from the ReTrace dashboard.")
            if request.headers.get("x-retrace") != "1":
                return refuse("That request did not come from the ReTrace dashboard.")
        elif request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            if not on_lan or request.headers.get("x-retrace") != "1":
                return refuse("That request did not come from the ReTrace dashboard.")
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    def problem(status: int, code: str, text: str) -> JSONResponse:
        return JSONResponse({"detail": text, "code": code}, status_code=status)

    async def run(name: str, *args):
        """The engine's answer, or the response that tells the dashboard why there is none."""
        try:
            return await asyncio.to_thread(engine.submit, name, *args)
        except UserError as error:
            return problem(409, error.code, str(error))
        except TimeoutError:
            return problem(503, "busy", "ReTrace is busy and did not answer in 10 s. Try again.")
        except (sqlite3.Error, OSError) as error:
            traceback.print_exc()
            return problem(
                500,
                "storage_error",
                f"Filesystem error: ReTrace could not read or write {store.root} ({type(error).__name__}: {error}).",
            )
        except Exception as error:
            traceback.print_exc()
            return problem(
                500,
                "internal_error",
                f"ReTrace hit an internal error ({type(error).__name__}: {error}). It is still running; "
                "the details are in the window it was started from.",
            )

    async def command(name: str, *args) -> JSONResponse:
        result = await run(name, *args)
        return result if isinstance(result, JSONResponse) else JSONResponse(result)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, _error: RequestValidationError) -> JSONResponse:
        if request.url.path == "/api/items":
            return problem(422, "invalid_name", "Invalid item name: send the name as text.")
        return problem(422, "invalid_request", "ReTrace did not understand that request.")

    @app.get("/")
    def index() -> Response:
        return Response((WEB_DIR / "index.html").read_bytes(), media_type="text/html; charset=utf-8")

    @app.get("/remote")
    def remote(request: Request) -> Response:
        if not lan or presented_token(request) != token:
            return refuse("Open the phone page with its token.")
        page = (WEB_DIR / "remote.html").read_text(encoding="utf-8")
        # The phone is not localhost, so the stylesheet and script need the same token
        # as the page. A plain /static request from the hotspot would be refused.
        page = page.replace("/static/remote.css", f"/static/remote.css?token={token}")
        page = page.replace("/static/remote.js", f"/static/remote.js?token={token}")
        return Response(page, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/health")
    def health() -> JSONResponse:
        return JSONResponse({"ok": True, "ring_connected": link.status()["connected"]})

    @app.get("/api/state")
    def state() -> JSONResponse:
        return JSONResponse(engine.snapshot())

    @app.get("/api/keyframes/{keyframe_id}.jpg")
    def keyframe(keyframe_id: int) -> Response:
        jpeg = store.jpeg(keyframe_id)
        if jpeg is None:
            return Response(status_code=404)
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/items/{item_id}")
    async def item_detail(item_id: int) -> JSONResponse:
        return await command("item_detail", item_id)

    @app.get("/body")
    def body_page() -> Response:
        return Response((WEB_DIR / "body.html").read_bytes(), media_type="text/html; charset=utf-8")

    @app.get("/app")
    def phone_app() -> Response:
        return Response((WEB_DIR / "phone.html").read_bytes(), media_type="text/html; charset=utf-8")

    @app.get("/manifest.webmanifest")
    def web_manifest() -> Response:
        return Response(
            (WEB_DIR / "manifest.webmanifest").read_bytes(),
            media_type="application/manifest+json",
        )

    @app.get("/icons/icon-192.png")
    def icon_192() -> Response:
        return Response((WEB_DIR / "icons" / "icon-192.png").read_bytes(), media_type="image/png")

    @app.get("/icons/icon-512.png")
    def icon_512() -> Response:
        return Response((WEB_DIR / "icons" / "icon-512.png").read_bytes(), media_type="image/png")

    @app.get("/apple-touch-icon.png")
    def apple_touch_icon() -> Response:
        return Response((WEB_DIR / "icons" / "apple-touch-icon.png").read_bytes(), media_type="image/png")

    @app.post("/api/sensor-logger")
    async def sensor_logger(request: Request) -> JSONResponse:
        """Sensor Logger HTTP Push. Always 200 so the app's test push succeeds."""
        try:
            message = await request.json()
        except Exception:
            message = {}
        if not isinstance(message, dict):
            message = {}
        return await command("ingest_sensor_logger", message)

    class MetresBody(BaseModel):
        metres: float

    @app.post("/api/body/recalibrate")
    async def body_recalibrate() -> JSONResponse:
        return await command("body_recalibrate")

    @app.post("/api/body/calibrate/start")
    async def body_calibrate_start() -> JSONResponse:
        return await command("body_arm_calibration")

    @app.post("/api/body/calibrate")
    async def body_calibrate(body: MetresBody) -> JSONResponse:
        return await command("body_calibrate", body.metres)

    @app.post("/api/body/loop/start")
    async def body_loop_start() -> JSONResponse:
        return await command("body_loop_start")

    @app.post("/api/body/loop/end")
    async def body_loop_end() -> JSONResponse:
        return await command("body_loop_end")

    @app.get("/api/body/live")
    def body_live() -> JSONResponse:
        return JSONResponse(engine.body_live())

    @app.post("/api/body/save")
    async def body_save(body: SaveBody) -> JSONResponse:
        return await command("save_item", body.name)

    class FindBody(BaseModel):
        item_id: int

    @app.post("/api/body/find")
    async def body_find(body: FindBody) -> JSONResponse:
        return await command("start_find", body.item_id)

    @app.post("/api/body/find/stop")
    async def body_find_stop() -> JSONResponse:
        return await command("stop_find")

    @app.get("/api/find/replay.png")
    def find_replay_png() -> Response:
        try:
            payload = engine.body_replay("png")
        except UserError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return Response(payload, media_type="image/png")

    @app.get("/api/find/replay.gif")
    def find_replay_gif() -> Response:
        try:
            payload = engine.body_replay("gif")
        except UserError as error:
            return JSONResponse({"detail": str(error)}, status_code=400)
        return Response(payload, media_type="image/gif")

    @app.post("/api/items")
    async def save_item(body: SaveBody) -> JSONResponse:
        """Always creates the item when the name is valid, even if the camera cannot see well."""
        return await command("save_item", body.name)

    @app.post("/api/items/{item_id}/find")
    async def find_item(item_id: int) -> JSONResponse:
        return await command("start_find", item_id)

    @app.post("/api/items/{item_id}/trail/stop")
    async def stop_trail(item_id: int) -> JSONResponse:
        return await command("stop_trail", item_id)

    @app.post("/api/items/{item_id}/delete")
    async def delete_item(item_id: int) -> JSONResponse:
        return await command("delete_item", item_id)

    @app.post("/api/find/stop")
    async def stop_find() -> JSONResponse:
        return await command("stop_find")

    @app.post("/api/motor/enabled")
    async def motor_enabled(body: EnabledBody) -> JSONResponse:
        return await command("set_motor_enabled", body.enabled)

    @app.post("/api/motor/test")
    async def motor_test() -> JSONResponse:
        return await command("test_pulse")

    @app.post("/api/putdown/mark")
    async def mark_putdown() -> JSONResponse:
        """Ground-truth put-down for tuning the IMU detector."""
        engine.note_remote_mark()
        return await command("mark_putdown")

    @app.post("/api/pdr/start")
    async def pdr_start(body: PdrStartBody) -> JSONResponse:
        """Start calibration or a loop on the service that is already running."""
        return await command("pdr_start", body.kind)

    @app.post("/api/pdr/end")
    async def pdr_end(body: PdrEndBody) -> JSONResponse:
        """End the walk. Calibration needs the distance; a loop needs two Ends."""
        return await command("pdr_end", body.meters)

    @app.post("/api/pdr/cancel")
    async def pdr_cancel() -> JSONResponse:
        return await command("pdr_cancel")

    @app.post("/api/pdr/resume-camera")
    async def pdr_resume_camera() -> JSONResponse:
        """Turn pictures back on after a PDR session that left them paused."""
        return await command("pdr_resume_camera")

    @app.websocket("/ws")
    async def live(websocket: WebSocket) -> None:
        headers = websocket.headers
        if headers.get("host", "").lower() not in hosts or headers.get("origin") not in origins:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        seen = 0
        state_at = 0.0
        try:
            while True:
                frame = link.latest_jpeg()
                if frame is not None and frame[0] != seen:
                    seen = frame[0]
                    await websocket.send_bytes(frame[1])
                now = time.monotonic()
                if now - state_at >= 0.25:
                    state_at = now
                    await websocket.send_text(json.dumps(engine.snapshot()))
                await asyncio.sleep(0.07)
        except (WebSocketDisconnect, RuntimeError):
            pass

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


def port_free(port: int) -> bool:
    """False when this port is taken on localhost or on every address.

    A listener on 0.0.0.0 still leaves a 127.0.0.1 bind free on Windows, so both are checked.
    """
    for host in ("127.0.0.1", "0.0.0.0"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                return False
    return True


def resolve_memory_dir(config: Config, cli_data: str | None = None) -> Path:
    """CLI --data wins; else config.data_dir; else the default under recordings/."""
    if cli_data:
        return Path(cli_data).expanduser().resolve()
    if config.data_dir:
        return Path(config.data_dir).expanduser().resolve()
    return Path(DATA_DIR).resolve()


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "weartest":
        from ring.weartest import run_weartest

        return run_weartest(argv[1:])
    if argv and argv[0] == "vlm-test":
        from ring.hold_replay import run_vlm_test

        return run_vlm_test(argv[1:])
    if argv and argv[0] == "replay-holds":
        from ring.hold_replay import run_replay_holds

        return run_replay_holds(argv[1:])
    if argv and argv[0] == "replay-find":
        from ring.replay_find import run_replay_find

        return run_replay_find(argv[1:])
    if argv and argv[0] == "replay-pdr":
        from ring.pdr_cli import run_replay_pdr

        return run_replay_pdr(argv[1:])
    if argv and argv[0] == "pdr-calib":
        from ring.pdr_cli import run_pdr_calib

        return run_pdr_calib(argv[1:])
    if argv and argv[0] == "pdr-loop-test":
        from ring.pdr_cli import run_pdr_loop_test

        return run_pdr_loop_test(argv[1:])
    if argv and argv[0] == "pdr-selftest":
        from ring.pdr_cli import run_pdr_selftest

        return run_pdr_selftest(argv[1:])
    if argv and argv[0] == "pdr-tune":
        from ring.pdr_tune import run_pdr_tune

        return run_pdr_tune(argv[1:])
    if argv and argv[0] == "pdr-report":
        from ring.pdr_tune import run_pdr_report

        return run_pdr_report(argv[1:])
    if argv and argv[0] == "pdr-import":
        from ring.pdr_import import run_pdr_import

        return run_pdr_import(argv[1:])
    if argv and argv[0] == "pocket":
        from ring.pocket import main as pocket_main

        return pocket_main(argv[1:])

    parser = argparse.ArgumentParser(description="Run ReTrace for the ring: capture, memory, items, find, vibration.")
    parser.add_argument("--no-motor", action="store_true", help="never write to the ring, so it cannot vibrate")
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard automatically")
    parser.add_argument("--port", type=int, default=0, help="web port on this computer (default 8010)")
    parser.add_argument(
        "--data",
        default="",
        help="folder for the visual memory (default: recordings/ring_memory, or data_dir in ring_config.json)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(CONFIG_PATH if CONFIG_PATH.is_file() else None)
    except (ValueError, json.JSONDecodeError) as error:
        print(f"ring_config.json has a problem: {error}", file=sys.stderr)
        return 2
    port = args.port or config.port
    if not port_free(port):
        print(
            f"Port {port} on this computer is already in use. ReTrace may already be running: open http://127.0.0.1:{port}/ "
            "or close the other copy first.",
            file=sys.stderr,
        )
        return 2
    data_dir = resolve_memory_dir(config, args.data or None)
    store, link, engine = build(data_dir, config, allow_motor=not args.no_motor, echo=terminal_line)
    engine.config_path = CONFIG_PATH if CONFIG_PATH.is_file() else None
    app = create_app(store, link, engine, port)
    url = f"http://127.0.0.1:{port}/"
    phone_ip = push_ipv4()
    lines = [
                "",
                "ReTrace ring service",
                f"  Dashboard:  {url}",
                f"  Memory:     {store.root}",
    ]
    if config.ring_transport == "wifi":
        from ring.net import lan_ipv4

        lines.append(f"  Ring Wi-Fi: listening on {lan_ipv4()}:{config.ring_wifi_port} (retrace.local)")
    else:
        lines.append("  Ring link:  USB serial")
    if config.dashboard_lan:
        lines.append(f"  Phone:      {remote_url(port)}")
    if phone_ip == "127.0.0.1":
        lines.append("  Phone push: this laptop has no network address yet")
    else:
        lines.append(f"  Phone push: http://{phone_ip}:{port}/api/sensor-logger")
        lines.append(f"  Phone page: http://{phone_ip}:{port}/body")
    lines.extend(
        [
                "  Vibration:  " + ("OFF for this run (--no-motor). Nothing is written to the ring." if args.no_motor else
                                    "one 200 ms pulse per confirmed progress step or arrival, and for the Test button."),
                "  Stop:       press Ctrl+C in this window",
                "  Below: the same lines as the dashboard's Activity list. Lines marked !! are problems.",
                "",
    ]
    )
    print("\n".join(lines), flush=True)
    from ring.net import hotspot_warning

    warning = hotspot_warning()
    if warning:
        print(warning, flush=True)
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=bind_host(config), port=port, access_log=False, log_level="warning", ws_max_size=65536)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
