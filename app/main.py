"""ReTrace HTTP and WebSocket server.

Phone sockets may publish frames. Desktop sockets may read the latest frame.
The two roles use different random tokens created when the process starts.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.analysis import CSV_FIELDS, Analyzer
from app.experiment import GuidedExperiment
from app.hub import Hub, valid_frame
from app.places import Places
from app.xiao_source import XiaoSource

ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = ROOT / ".session-token"
TEMPLATES = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

log = logging.getLogger("retrace")
hub = Hub()
places = Places(ROOT / "recordings" / "places")
experiment = GuidedExperiment(ROOT / "recordings" / "places" / "experiments", busy=lambda: places.testing)
places.guided = experiment
analyzer = Analyzer(hub, on_frame=places.observe)
xiao = XiaoSource(hub)


class Session:
    phone_token: str = ""
    desktop_token: str = ""


session = Session()

NO_STORE = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' blob:; "
        "media-src 'self' blob:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}


def setup_logging() -> None:
    logger = logging.getLogger("retrace")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def write_session_file() -> None:
    """Atomically replace the local secret file. It is not served over HTTP."""
    session.phone_token = secrets.token_urlsafe(32)
    session.desktop_token = secrets.token_urlsafe(32)
    temporary = TOKEN_PATH.with_suffix(".token-tmp")
    temporary.write_text(
        session.phone_token + "\n" + session.desktop_token + "\n",
        encoding="utf-8",
    )
    temporary.replace(TOKEN_PATH)


def load_or_create_session() -> None:
    """Reuse the current session file so an already scanned QR code keeps working."""
    if TOKEN_PATH.exists():
        lines = [line.strip() for line in TOKEN_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        if len(lines) == 2 and all(8 <= len(line) <= 128 for line in lines):
            session.phone_token, session.desktop_token = lines
            log.info("Reusing the existing session file so the current QR code stays valid")
            return
    write_session_file()


def banner() -> None:
    print(
        "\n".join(
            [
                "",
                "ReTrace is running.",
                "  Desktop viewer: http://127.0.0.1:8000/desktop",
                "  Health check:   http://127.0.0.1:8000/health",
                f"  Session file:   {TOKEN_PATH}",
                "  Tunnel command: cloudflared tunnel --url http://127.0.0.1:8000",
                "  QR command:     python scripts/make_qr.py https://YOUR-TUNNEL.trycloudflare.com",
                "",
                "The session file is a secret. ReTrace does not print the tokens.",
                "Video frames stay in memory only and are not written to disk.",
                "",
            ]
        ),
        flush=True,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    setup_logging()
    load_or_create_session()
    analysis_task = asyncio.create_task(analyzer.run())
    banner()
    try:
        yield
    finally:
        if experiment.active:
            experiment.cancel = True
        if experiment.task is not None and not experiment.task.done():
            experiment.task.cancel()
        analysis_task.cancel()
        await xiao.stop()
        try:
            await analysis_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="ReTrace", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class NoCacheStatic(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


def tokens_match(provided: str, expected: str) -> bool:
    provided = provided.strip()
    if not provided or not expected or len(provided) > 128 or len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


def is_local_request(request: Request) -> bool:
    """True only for a browser on this machine talking to loopback directly.

    cloudflared also connects from loopback, so the peer address is not enough.
    Tunnel requests carry the public Host and Cloudflare forwarding headers.
    """
    headers = request.headers
    if any(
        headers.get(name)
        for name in (
            "cf-connecting-ip",
            "cf-ray",
            "x-forwarded-for",
            "x-forwarded-proto",
            "x-forwarded-host",
        )
    ):
        return False
    peer = request.client.host if request.client else ""
    if peer not in {"127.0.0.1", "::1"}:
        return False
    hostname = headers.get("host", "").split(":")[0].strip("[]").lower()
    return hostname in {"localhost", "127.0.0.1", "::1"}


def page(name: str, replacements: dict[str, str], headers: dict[str, str]) -> HTMLResponse:
    document = (TEMPLATES / name).read_text(encoding="utf-8")
    for key, value in replacements.items():
        document = document.replace(key, value)
    leftover = [key for key in replacements if key in document]
    if leftover:
        raise RuntimeError(f"Unreplaced template fields in {name}: {', '.join(leftover)}")
    return HTMLResponse(document, headers=headers)


async def receive_token(websocket: WebSocket, expected: str) -> bool:
    """Read the first text message and accept only the matching session token.

    The token stays out of the socket URL because the server logs that path.
    """
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=5)
    except (TimeoutError, WebSocketDisconnect):
        message = None
    provided = ""
    if message and message.get("type") != "websocket.disconnect":
        provided = message.get("text") or ""
    if tokens_match(provided, expected):
        return True
    try:
        await websocket.close(code=1008, reason="unauthorized")
    except Exception:
        pass
    return False


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    name = "index_local.html" if is_local_request(request) else "index_public.html"
    return page(name, {}, NO_STORE)


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


@app.get("/robots.txt")
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n", headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


def render_phone(token: str) -> HTMLResponse:
    if tokens_match(token, session.phone_token):
        auth = "ok"
        issued = session.phone_token
    elif token.strip():
        auth = "rejected"
        issued = ""
    else:
        auth = "missing"
        issued = ""
    headers = {**NO_STORE, "Permissions-Policy": "camera=(self), microphone=()"}
    return page(
        "phone.html",
        {
            "__AUTH__": auth,
            "__TOKEN__": html.escape(issued, quote=True),
        },
        headers,
    )


@app.get("/phone", response_class=HTMLResponse)
def phone_page(token: str = Query(default="")) -> HTMLResponse:
    return render_phone(token)


@app.get("/phone/{token}", response_class=HTMLResponse)
def phone_page_from_path(token: str) -> HTMLResponse:
    # The tunnel's warning button replaces the query string. A token in the
    # path is still there after that button is tapped.
    return render_phone(token)


@app.get("/desktop", response_class=HTMLResponse)
def desktop_page(request: Request, token: str = Query(default="")) -> HTMLResponse:
    # The desktop token is embedded only for a local browser, or for a caller
    # that already has it. The phone QR link never includes this token.
    allowed = is_local_request(request) or tokens_match(token, session.desktop_token)
    headers = {**NO_STORE, "Permissions-Policy": "camera=(), microphone=()"}
    return page(
        "desktop.html",
        {
            "__AUTH__": "ok" if allowed else "unauthorized",
            "__TOKEN__": html.escape(session.desktop_token if allowed else "", quote=True),
        },
        headers,
    )


def desktop_authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    provided = header[7:] if header.lower().startswith("bearer ") else ""
    return tokens_match(provided, session.desktop_token)


def phone_authorized(request: Request) -> bool:
    header = request.headers.get("authorization", "")
    provided = header[7:] if header.lower().startswith("bearer ") else ""
    return tokens_match(provided, session.phone_token)


def phone_place_view(state: dict) -> dict:
    """What the phone may see. No laptop log download and no saved descriptors."""
    decision = state.get("decision")
    slim = None
    if decision:
        slim = {
            "predicted": decision.get("predicted", "UNKNOWN"),
            "stable_predicted": decision.get("stable_predicted", "UNKNOWN"),
            "failure": decision.get("failure", ""),
            "failure_text": decision.get("failure_text", ""),
        }
    return {
        "testing": state.get("testing", False),
        "segment": state.get("segment", ""),
        "references": state.get("references", {}),
        "decision": slim,
        "last_mark": state.get("last_mark"),
        "last_archive": state.get("last_archive") or "",
    }


def unauthorized() -> JSONResponse:
    return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"})


class RecordingStart(BaseModel):
    condition: str


class PlaceSave(BaseModel):
    place: str
    camera: str
    save_images: bool = False


class PlaceClear(BaseModel):
    place: str


class PlaceMask(BaseModel):
    enabled: bool
    x: float = 0
    y: float = 0
    w: float = 0
    h: float = 0


class PlaceTestStart(BaseModel):
    camera: str
    save_images: bool = False
    ground_truth: str = ""


class PlaceEvent(BaseModel):
    event: str


class DiagnosticStart(BaseModel):
    camera: str = "palm"
    save_unknown: bool = False


class UnknownShots(BaseModel):
    enabled: bool


class IntervalNote(BaseModel):
    label: str
    start_unix_ms: int
    end_unix_ms: int


class IntervalNotes(BaseModel):
    intervals: list[IntervalNote] = []


class ExperimentStart(BaseModel):
    away_seconds: int = 8
    back_seconds: int = 8
    views: str = "multi"


class ExperimentArrival(BaseModel):
    session_id: str
    late_seconds: int = 0
    trial: int = 0


def place_error(error: Exception) -> JSONResponse:
    status = 409 if isinstance(error, RuntimeError) else 400
    return JSONResponse({"detail": str(error)}, status_code=status, headers={"Cache-Control": "no-store"})


class CameraSelect(BaseModel):
    source: str = "iphone"


async def camera_status() -> dict:
    payload = await hub.status()
    payload.update(await xiao.status())
    analysis = await analyzer.snapshot()
    payload["processing_fps"] = analysis["analysis_fps"]
    metrics = analysis.get("metrics") or {}
    payload["orb_features"] = metrics.get("feature_count")
    payload["frame_width"] = metrics.get("width")
    payload["frame_height"] = metrics.get("height")
    decision = (await places.snapshot()).get("decision") or {}
    payload["recognition"] = decision.get("predicted") or ""
    payload["recognition_stable"] = decision.get("stable_predicted") or ""
    payload["recognition_inliers"] = (decision.get("inliers") or {}).get("A", "")
    return payload


@app.get("/api/status")
async def status(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await camera_status(), headers={"Cache-Control": "no-store"})


@app.post("/api/camera")
async def select_camera(body: CameraSelect, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    source = body.source
    if source not in {"iphone", "xiao_usb"}:
        return JSONResponse(
            {"detail": "Choose the iPhone camera or the XIAO USB camera."},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    if source == "iphone":
        await xiao.stop()
        await hub.set_source("iphone")
        return JSONResponse(await camera_status(), headers={"Cache-Control": "no-store"})
    await hub.set_source("xiao_usb")
    try:
        await xiao.start()
    except RuntimeError as error:
        await xiao.stop()
        await hub.set_source("iphone")
        return JSONResponse({"detail": str(error)}, status_code=409, headers={"Cache-Control": "no-store"})
    return JSONResponse(await camera_status(), headers={"Cache-Control": "no-store"})


@app.get("/api/analysis")
async def analysis(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await analyzer.snapshot(), headers={"Cache-Control": "no-store"})


@app.post("/api/recording/start")
async def recording_start(body: RecordingStart, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await analyzer.start_recording(body.condition)
    except ValueError as error:
        return JSONResponse({"detail": str(error)}, status_code=400, headers={"Cache-Control": "no-store"})
    except RuntimeError as error:
        return JSONResponse({"detail": str(error)}, status_code=409, headers={"Cache-Control": "no-store"})
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/recording/stop")
async def recording_stop(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await analyzer.stop_recording(), headers={"Cache-Control": "no-store"})


@app.get("/api/recording.csv")
async def recording_csv(request: Request) -> Response:
    if not desktop_authorized(request):
        return unauthorized()
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(await analyzer.export_rows())
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="retrace-feasibility.csv"',
        },
    )


@app.get("/api/places")
async def places_state(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await places.snapshot(), headers={"Cache-Control": "no-store"})


@app.post("/api/places/reference")
async def places_save_reference(body: PlaceSave, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.save_reference(body.place, body.camera, body.save_images)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/clear")
async def places_clear(body: PlaceClear, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.clear_place(body.place)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/mask")
async def places_mask(body: PlaceMask, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.set_mask(body.enabled, body.x, body.y, body.w, body.h)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/test/start")
async def places_test_start(body: PlaceTestStart, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.start_test(body.camera, body.save_images)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/diagnostic/start")
async def places_diagnostic_start(body: DiagnosticStart, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.start_diagnostic(body.camera, body.save_unknown)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/diagnostic/shots")
async def places_diagnostic_shots(body: UnknownShots, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await places.set_save_unknown(body.enabled), headers={"Cache-Control": "no-store"})


@app.post("/api/places/intervals")
async def places_intervals(body: IntervalNotes, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.annotate_intervals([item.model_dump() for item in body.intervals])
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/places/reference/{reference_id}")
async def places_reference_image(reference_id: str, request: Request) -> Response:
    if not desktop_authorized(request):
        return unauthorized()
    jpeg = places.reference_jpeg(reference_id)
    if jpeg is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/places/test/event")
async def places_test_event(body: PlaceEvent, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.mark_event(body.event)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/places/test/stop")
async def places_test_stop(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await places.stop_test(), headers={"Cache-Control": "no-store"})


@app.post("/api/places/test/clear")
async def places_test_clear(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await places.clear_tests()
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/places/results.csv")
async def places_csv(request: Request) -> Response:
    if not desktop_authorized(request):
        return unauthorized()
    text = places.csv_text(await places.export_rows())
    return Response(
        text,
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="retrace-places.csv"',
        },
    )


@app.get("/api/phone/places")
async def phone_places_state(request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    return JSONResponse(phone_place_view(await places.snapshot()), headers={"Cache-Control": "no-store"})


@app.post("/api/phone/test/start")
async def phone_test_start(request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    try:
        payload = await places.start_test("palm", False)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(phone_place_view(payload), headers={"Cache-Control": "no-store"})


@app.post("/api/phone/test/event")
async def phone_test_event(body: PlaceEvent, request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    try:
        payload = await places.mark_event(body.event)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(phone_place_view(payload), headers={"Cache-Control": "no-store"})


@app.post("/api/phone/test/stop")
async def phone_test_stop(request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    return JSONResponse(phone_place_view(await places.stop_test()), headers={"Cache-Control": "no-store"})


@app.post("/api/phone/experiment/start")
async def phone_experiment_start(body: ExperimentStart, request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    try:
        payload = await experiment.start(body.away_seconds, body.back_seconds, body.views)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/phone/experiment/stop")
async def phone_experiment_stop(request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    return JSONResponse(await experiment.stop(), headers={"Cache-Control": "no-store"})


@app.get("/api/phone/experiment")
async def phone_experiment_state(request: Request) -> JSONResponse:
    if not phone_authorized(request):
        return unauthorized()
    return JSONResponse(await experiment.snapshot(phone=True), headers={"Cache-Control": "no-store"})


@app.get("/api/places/experiment")
async def places_experiment_state(request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    return JSONResponse(await experiment.snapshot(phone=False), headers={"Cache-Control": "no-store"})


@app.post("/api/places/experiment/arrival")
async def places_experiment_arrival(body: ExperimentArrival, request: Request) -> JSONResponse:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        payload = await experiment.correct_arrival(body.session_id, body.late_seconds, body.trial)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.get("/api/places/experiment.csv")
async def places_experiment_csv(request: Request, session_id: str = "") -> Response:
    if not desktop_authorized(request):
        return unauthorized()
    try:
        text = await experiment.export_csv(session_id)
    except (ValueError, RuntimeError) as error:
        return place_error(error)
    name = session_id or "latest"
    return Response(
        text,
        media_type="text/csv",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="retrace-experiment-{name}.csv"',
        },
    )


@app.get("/api/places/experiment/{session_id}/reference/{reference_id}")
async def places_experiment_reference(session_id: str, reference_id: str, request: Request) -> Response:
    if not desktop_authorized(request):
        return unauthorized()
    jpeg = experiment.reference_jpeg(session_id, reference_id)
    if jpeg is None:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/ws/places")
async def places_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    if not await receive_token(websocket, session.desktop_token):
        log.warning("Rejected place viewer")
        return
    log.info("Place viewer connected")
    seen = 0
    try:
        while True:
            item = await places.next_packet(seen, timeout=15)
            if item is None:
                await websocket.send_json({"type": "ping"})
                continue
            payload, seen = item
            await websocket.send_bytes(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Place viewer failed")
    finally:
        log.info("Place viewer disconnected")


@app.websocket("/ws/phone")
async def phone_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    if not await receive_token(websocket, session.phone_token):
        log.warning("Rejected phone connection")
        return
    await hub.attach_phone(websocket)
    log.info("Phone connected")
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            payload = message.get("bytes")
            if payload is None or not valid_frame(payload):
                continue
            await hub.publish(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Phone connection failed")
    finally:
        await hub.detach_phone(websocket)
        log.info("Phone disconnected")


@app.websocket("/ws/desktop")
async def desktop_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    if not await receive_token(websocket, session.desktop_token):
        log.warning("Rejected desktop connection")
        return
    await hub.change_desktop_viewers(1)
    log.info("Desktop viewer connected")
    seen = 0
    try:
        while True:
            item = await hub.next_frame(seen, timeout=15)
            if item is None:
                await websocket.send_json({"type": "ping"})
                continue
            payload, seen = item
            await websocket.send_bytes(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Desktop viewer failed")
    finally:
        await hub.change_desktop_viewers(-1)
        log.info("Desktop viewer disconnected")


@app.websocket("/ws/analysis")
async def analysis_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    if not await receive_token(websocket, session.desktop_token):
        log.warning("Rejected analysis connection")
        return
    log.info("Analysis viewer connected")
    seen = 0
    try:
        while True:
            item = await analyzer.next_annotated(seen, timeout=15)
            if item is None:
                await websocket.send_json({"type": "ping"})
                continue
            payload, seen = item
            await websocket.send_bytes(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Analysis viewer failed")
    finally:
        log.info("Analysis viewer disconnected")


app.mount("/static", NoCacheStatic(directory=STATIC_DIR), name="static")
