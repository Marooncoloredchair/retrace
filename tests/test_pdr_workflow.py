"""PDR tests stay inside the running service, and they refuse to start when the ring is quiet."""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import uvicorn

from ring.embedder import Embedder
from ring.engine import UserError
from ring.net import hotspot_warning
from ring.pdr_cli import run_pdr_calib, run_pdr_loop_test
from ring.pdr_live import (
    ERR_HOTSPOT,
    ERR_PICTURES,
    ERR_RATE,
    ERR_SERIAL,
    ERR_SOCKET,
    describe_drops,
    diagnose,
)
from ring.service import build, create_app
from tests.ring_sim import Rig, SimRing, World, wait_until


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "ring_config.json"


def _post(port: int, path: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-Retrace": "1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def _get(port: int, path: str) -> tuple[int, dict | str]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        if path.startswith("/api/"):
            return response.status, json.loads(raw.decode())
        return response.status, raw.decode()


class PreflightTextTest(unittest.TestCase):
    def test_low_rate_names_the_reason(self) -> None:
        self.assertIsNone(
            diagnose(imu_hz=80, hotspot=False, transport="serial", connected=False, camera_fps=20, dropped_delta=5)
        )
        self.assertEqual(
            diagnose(imu_hz=12, hotspot=True, transport="wifi", connected=True, camera_fps=0, dropped_delta=0),
            ERR_RATE,
        )
        self.assertEqual(
            diagnose(imu_hz=0, hotspot=False, transport="wifi", connected=False, camera_fps=0, dropped_delta=0),
            ERR_HOTSPOT,
        )
        self.assertEqual(
            diagnose(imu_hz=10, hotspot=True, transport="serial", connected=True, camera_fps=0, dropped_delta=0),
            ERR_SERIAL,
        )
        self.assertEqual(
            diagnose(imu_hz=0, hotspot=True, transport="wifi", connected=False, camera_fps=0, dropped_delta=0),
            ERR_SOCKET,
        )
        self.assertEqual(
            diagnose(imu_hz=20, hotspot=True, transport="wifi", connected=True, camera_fps=8, dropped_delta=0),
            ERR_PICTURES,
        )


class HotspotWarningTest(unittest.TestCase):
    def test_warning_names_the_hotspot_and_not_a_password(self) -> None:
        text = hotspot_warning(False)
        self.assertIn("!! HOTSPOT IS OFF !!", text)
        self.assertIn("192.168.137.1", text)
        self.assertIn("start_laptop_hotspot.ps1", text)
        self.assertIn("10.28.46.136", text)
        self.assertNotIn("password", text.lower())
        self.assertEqual(hotspot_warning(True), "")


class EmbedderLazyTest(unittest.TestCase):
    def test_model_stays_unloaded_until_a_place_find_embed(self) -> None:
        calls: list[str] = []
        embedder = Embedder(lambda *_args: None, enabled=True)

        def _load() -> None:
            calls.append("load")
            embedder.ready = True
            embedder.model = object()

        embedder._load = _load
        embedder.start()
        try:
            self.assertTrue(embedder.submit(1, b"\xff\xd8", 1.0))
            time.sleep(0.35)
            self.assertEqual(calls, [])
            self.assertIsNone(embedder.model)
            self.assertFalse(embedder.ready)
            embedder.hold_load = True
            self.assertIsNone(embedder.embed_bytes(b"\xff\xd8"))
            self.assertEqual(calls, [])
            embedder.hold_load = False
            embedder.embed_bytes(b"\xff\xd8")
            self.assertEqual(calls, ["load"])
        finally:
            embedder.stop()


class ServiceRefusesTest(unittest.TestCase):
    def test_cli_does_not_start_a_second_server(self) -> None:
        port = free_port()

        def refuse_input(*_args, **_kwargs):
            raise AssertionError("asked for metres")

        def refuse_http(*_args, **_kwargs):
            raise AssertionError("talked to a server")

        err = io.StringIO()
        with patch("builtins.input", refuse_input), patch("ring.pdr_cli._service", refuse_http), redirect_stderr(err):
            calib = run_pdr_calib(["--port", str(port), "--meters", "10"])
            loop = run_pdr_loop_test(["--label", "swing", "--port", str(port)])
        self.assertEqual((calib, loop), (2, 2))
        text = err.getvalue()
        self.assertIn(f"Start ReTrace first. Nothing is listening on port {port}.", text)
        self.assertNotIn("metres", text.lower())
        with socket.socket() as probe:
            probe.settimeout(0.3)
            with self.assertRaises(OSError):
                probe.connect(("127.0.0.1", port))

    def test_preflight_failure_prints_the_reason_and_does_not_ask(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                payload = json.dumps({"detail": ERR_RATE, "code": "ring_not_connected"}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, _format: str, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        err = io.StringIO()
        try:

            def refuse_input(*_args, **_kwargs):
                raise AssertionError("asked for metres")

            with patch("builtins.input", refuse_input), redirect_stderr(err):
                code = run_pdr_calib(["--port", str(port), "--meters", "12"])
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 2)
        self.assertIn(ERR_RATE, err.getvalue())


class DropReportTest(unittest.TestCase):
    def test_report_places_drops_against_the_taps_and_names_an_open_gate(self) -> None:
        taps = [{"name": "start", "unix": 100.0}, {"name": "end", "unix": 110.0}]
        events = [
            {"count": 10, "unix": 102.0, "gate_open": False},
            {"count": 4, "unix": 108.0, "gate_open": True},
            {"count": 7, "unix": 111.5, "gate_open": False},
        ]
        text = describe_drops(events, taps)
        self.assertIn("Dropped 21 IMU samples:", text)
        self.assertIn("10 at 2.0 s after Start", text)
        self.assertIn("4 at 8.0 s after Start, while the walking gate was open", text)
        self.assertIn("7 at 1.5 s after End", text)
        self.assertIn("Dropped samples while the walking gate was open: 4.", text)

    def test_no_drops_is_a_plain_sentence(self) -> None:
        self.assertEqual(describe_drops([], [{"name": "start", "unix": 1.0}]), "No IMU samples were dropped.")


class EnginePdrTest(unittest.TestCase):
    def setUp(self) -> None:
        self.real = REAL_CONFIG.read_bytes()
        self.folder = Path(tempfile.mkdtemp(prefix="retrace-pdr-"))
        self.config_path = self.folder / "ring_config.json"
        self.config_path.write_text(
            json.dumps({"find_backend": "place", "pdr_step_length": 0.93, "dashboard_lan": True}) + "\n",
            encoding="utf-8",
        )
        self.reports = self.folder / "reports"
        self.reports.mkdir()
        self.rig = Rig(find_backend="place", embedder_enabled=True)
        self.rig.engine.config_path = self.config_path
        self.rig.engine.pdr_report_root = self.reports
        self.rig.link.transport = "wifi"
        self.rig.link.camera_fps = 0.0
        self.loads: list[str] = []

        def _load() -> None:
            self.loads.append("load")
            raise AssertionError("DINOv2 loaded during PDR")

        self.rig.engine.embedder._load = _load

    def _open_walk(self) -> None:
        self.rig.link.imu_rate_hz = 100.0
        self.rig.wait(0.01)
        self.rig.wait(1.0)

    def tearDown(self) -> None:
        self.rig.close()
        shutil.rmtree(self.folder, ignore_errors=True)
        self.assertEqual(REAL_CONFIG.read_bytes(), self.real)

    def test_low_imu_rate_waits_and_does_not_save_or_show_the_walk(self) -> None:
        self.rig.link.imu_rate_hz = 12.0
        started = self.rig.engine.pdr_start("calib")
        self.assertEqual(started["phase"], "settling")
        self.assertTrue(started["camera_paused"])
        self.rig.wait(1.2)
        self.assertEqual(self.rig.engine.pdr_run["phase"], "settling")
        self.assertEqual(bytes(self.rig.link.outbox), b"i")
        self.assertTrue(self.rig.engine.embedder.hold_load)
        texts = " ".join(note["text"] for note in self.rig.engine.notices)
        self.assertIn("Waiting until the motion stream stays above 80 Hz", texts)
        self.assertNotIn("Walk a known distance", texts)
        self.assertEqual(self.loads, [])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["find_backend"], "place")
        self.assertEqual(saved["pdr_step_length"], 0.93)

    def test_hotspot_off_refuses_before_the_walk_and_leaves_pictures_paused(self) -> None:
        self.rig.link.connected = False
        self.rig.link.transport = "wifi"
        with patch("ring.net.hotspot_up", return_value=False):
            with self.assertRaises(UserError) as caught:
                self.rig.engine.pdr_start("calib")
        self.assertEqual(str(caught.exception), ERR_HOTSPOT)
        self.assertIsNone(self.rig.engine.pdr_run)
        self.assertEqual(bytes(self.rig.link.outbox), b"i")
        self.assertTrue(self.rig.engine.camera_paused)

    def test_a_dip_restarts_the_one_second_wait(self) -> None:
        self.rig.link.imu_rate_hz = 100.0
        self.rig.engine.pdr_start("calib")
        self.rig.wait(0.01)
        self.rig.wait(0.6)
        self.assertEqual(self.rig.engine.pdr_run["phase"], "settling")
        self.rig.link.imu_rate_hz = 10.0
        self.rig.wait(0.1)
        self.rig.link.imu_rate_hz = 100.0
        self.rig.wait(0.01)
        self.rig.wait(0.9)
        self.assertEqual(self.rig.engine.pdr_run["phase"], "settling")
        self.rig.wait(0.1)
        self.assertEqual(self.rig.engine.pdr_run["phase"], "walk")
        texts = " ".join(note["text"] for note in self.rig.engine.notices)
        self.assertIn("Walk a known distance", texts)

    def test_end_freezes_steps_and_leaves_the_camera_paused(self) -> None:
        started = self.rig.engine.pdr_start("calib")
        self.assertEqual(started["phase"], "settling")
        self._open_walk()
        self.assertEqual(self.rig.engine.pdr_run["phase"], "walk")
        self.assertEqual(self.loads, [])
        self.assertTrue(self.rig.engine.embedder.hold_load)
        tracker = self.rig.engine.pdr_run["tracker"]
        tracker.steps = 3
        self.rig.engine.mark_putdown()
        self.assertTrue(tracker.frozen)
        self.assertEqual(self.rig.engine.pdr_run["steps_frozen"], 3)
        tracker.steps = 99
        tracker.push({"ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0, "gx_dps": 0.0, "gy_dps": 0.0, "gz_dps": 0.0, "host_unix": 10})
        ended = self.rig.engine.pdr_end(6)
        self.assertEqual(ended["phase"], "done")
        self.assertEqual(ended["step_length"], 2.0)
        self.assertEqual(ended["steps"], 3)
        self.assertTrue(self.rig.engine.embedder.hold_load)
        self.assertTrue(self.rig.engine.camera_paused)
        self.assertEqual(bytes(self.rig.link.outbox), b"i")
        resumed = self.rig.engine.pdr_resume_camera()
        self.assertFalse(resumed["camera_paused"])
        self.assertFalse(self.rig.engine.embedder.hold_load)
        self.assertTrue(bytes(self.rig.link.outbox).endswith(b"c"))
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["find_backend"], "place")
        self.assertEqual(saved["pdr_step_length"], 2.0)
        self.assertTrue(saved["dashboard_lan"])
        state = self.rig.engine.publish()
        self.assertNotIn("path", state["pdr"])
        self.assertIn("walking", state["pdr"])
        self.assertIn("steps", state["pdr"])
        self.assertIn("path_m", state["pdr"])
        self.assertEqual(self.loads, [])

    def test_setting_false_turns_the_camera_back_on_at_the_end(self) -> None:
        self.rig.engine.config.pdr_session_keep_camera_paused = False
        self.rig.link.imu_rate_hz = 100.0
        self.rig.engine.pdr_start("calib")
        self._open_walk()
        self.rig.engine.pdr_run["tracker"].steps = 2
        ended = self.rig.engine.pdr_end(4)
        self.assertEqual(ended["phase"], "done")
        self.assertFalse(self.rig.engine.camera_paused)
        self.assertFalse(self.rig.engine.embedder.hold_load)
        self.assertTrue(bytes(self.rig.link.outbox).endswith(b"c"))

    def test_drops_during_an_open_gate_are_named_against_the_taps(self) -> None:
        self.rig.link.imu_rate_hz = 100.0
        self.rig.engine.pdr_start("calib")
        self._open_walk()
        tracker = self.rig.engine.pdr_run["tracker"]
        tracker.walking = True
        self.rig.link.imu_dropped = 5
        self.rig.wait(0.2)
        self.rig.wait(0.05)
        tracker.steps = 4
        ended = self.rig.engine.pdr_end(8)
        self.assertIn("while the walking gate was open", ended["drop_report"])
        self.assertIn("after Start", ended["drop_report"])
        self.assertIn("Dropped samples while the walking gate was open: 5.", ended["drop_report"])
        self.rig.link.imu_dropped = 8
        self.rig.wait(0.3)
        report = self.rig.engine.pdr_run["drop_report"]
        self.assertIn("after End", report)
        self.assertIn("Dropped samples while the walking gate was open: 5.", report)
        self.assertNotIn("Dropped samples while the walking gate was open: 8.", report)

    def test_loop_end_reports_closure(self) -> None:
        self.rig.link.imu_rate_hz = 100.0
        self.rig.engine.pdr_start("carry")
        self._open_walk()
        first = self.rig.engine.pdr_end(None)
        self.assertEqual(first["marks"], 1)
        self.assertFalse(self.rig.engine.pdr_run["tracker"].frozen)
        second = self.rig.engine.pdr_end(None)
        self.assertEqual(second["phase"], "done")
        self.assertEqual(second["closure_error_m"], 0)
        self.assertEqual(second["marks"], 2)
        self.assertTrue(self.rig.engine.pdr_run["tracker"].frozen)
        self.assertTrue(self.rig.engine.camera_paused)
        self.assertEqual(bytes(self.rig.link.outbox), b"i")
        summaries = list(self.reports.glob("pdr_loop_carry_*/summary.json"))
        self.assertEqual(len(summaries), 1)
        report = json.loads(summaries[0].read_text(encoding="utf-8"))
        self.assertEqual(report["truth_closure_m"], 0)
        self.assertEqual(report["label"], "carry")
        self.assertIn("drop_report", report)
        self.assertEqual(self.loads, [])


class PhoneApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.real = REAL_CONFIG.read_bytes()
        cls.folder = Path(tempfile.mkdtemp(prefix="retrace-pdr-api-"))
        cls.config_path = cls.folder / "ring_config.json"
        cls.config_path.write_text(
            json.dumps({"find_backend": "place", "pdr_step_length": 0.93}) + "\n",
            encoding="utf-8",
        )
        cls.reports = cls.folder / "reports"
        cls.reports.mkdir()
        cls.world = World(9)
        cls.ring = SimRing(cls.world.view(0))
        cls.port = free_port()
        cls.loads: list[str] = []
        cls._load = Embedder._load

        def _load(self) -> None:
            cls.loads.append("load")
            raise AssertionError("DINOv2 loaded")

        Embedder._load = _load
        from ring.config import Config

        config = Config(
            embedder_enabled=True,
            dashboard_lan=True,
            reconnect_seconds=0.1,
            stall_seconds=1.0,
            vlm_provider="none",
        )
        cls._token = patch("ring.net.ensure_dashboard_token", return_value="abc123")
        cls._rate = patch("ring.pdr_live.imu_rate_hz", return_value=12.0)
        cls._hotspot = patch("ring.net.hotspot_up", return_value=True)
        cls._token.start()
        cls.rate_mock = cls._rate.start()
        cls._hotspot.start()
        store, link, engine = build(cls.folder, config, opener=cls.ring.opener)
        cls.link = link
        cls.engine = engine
        engine.config_path = cls.config_path
        engine.pdr_report_root = cls.reports
        cls.server = uvicorn.Server(
            uvicorn.Config(create_app(store, link, engine, cls.port), host="127.0.0.1", port=cls.port, log_level="error")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        assert wait_until(lambda: cls.server.started, 10.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        Embedder._load = cls._load
        cls._rate.stop()
        cls._hotspot.stop()
        cls._token.stop()
        shutil.rmtree(cls.folder, ignore_errors=True)
        if REAL_CONFIG.read_bytes() != cls.real:
            REAL_CONFIG.write_bytes(cls.real)
            raise AssertionError("ring_config.json was modified")

    def test_phone_start_end_and_preflight(self) -> None:
        status, page = _get(self.port, "/remote?token=abc123")
        self.assertEqual(status, 200)
        self.assertIsInstance(page, str)
        for label in ("Start calibration", "Start swing loop", "Start carry loop", "End", "Resume camera", "Distance in metres"):
            self.assertIn(label, page)
        self.assertIn("/static/remote.js?token=abc123", page)
        self.assertIn("/static/remote.css?token=abc123", page)
        script_request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/static/remote.js?token=abc123",
            headers={"Host": f"192.168.137.50:{self.port}"},
        )
        with urllib.request.urlopen(script_request, timeout=15) as response:
            script_body = response.read().decode()
            self.assertEqual(response.status, 200)
        self.assertIn("Walking gate:", script_body)
        blocked = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/static/remote.js",
            headers={"Host": f"192.168.137.50:{self.port}"},
        )
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(blocked, timeout=15)
        self.assertEqual(refused.exception.code, 403)
        script = (ROOT / "ring" / "web" / "remote.js").read_text(encoding="utf-8")
        for label in ("Ring connected:", "Camera:", "IMU rate:", "Walking gate:", "/api/pdr/start", "/api/pdr/end", "/api/pdr/resume-camera"):
            self.assertIn(label, script)

        self.assertTrue(wait_until(lambda: self.link.status()["connected"], 5.0))
        status, started = _post(self.port, "/api/pdr/start", {"kind": "calib"})
        self.assertEqual(status, 200, started)
        self.assertEqual(started["phase"], "settling")
        self.assertTrue(started["camera_paused"])
        time.sleep(1.2)
        state_status, state = _get(self.port, "/api/state")
        self.assertEqual(state_status, 200)
        self.assertEqual(state["pdr_test"]["phase"], "settling")
        self.assertNotIn(b"c", bytes(self.ring.written))
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pdr_step_length"], 0.93)
        self.assertEqual(saved["find_backend"], "place")

        self.rate_mock.return_value = 100.0
        self.assertTrue(
            wait_until(lambda: _get(self.port, "/api/state")[1]["pdr_test"]["phase"] == "walk", 3.0),
            _get(self.port, "/api/state")[1]["pdr_test"],
        )
        state_status, state = _get(self.port, "/api/state")
        self.assertEqual(state_status, 200)
        self.assertNotIn("path", state["pdr"])
        self.assertIn("walking", state["pdr"])
        self.assertIn("steps", state["pdr"])
        self.assertIn(state["link"]["connected"], (True, False))
        self.assertIn("rate_hz", state["link"]["imu"])

        status, ended = _post(self.port, "/api/pdr/end", {"meters": 10})
        self.assertEqual(status, 200, ended)
        self.assertEqual(ended["phase"], "done")
        self.assertIn("not changed", ended["error"])
        self.assertNotIn(b"c", bytes(self.ring.written))
        self.assertIn("drop_report", ended)
        status, resumed = _post(self.port, "/api/pdr/resume-camera", {})
        self.assertEqual(status, 200, resumed)
        self.assertFalse(resumed["camera_paused"])
        self.assertTrue(wait_until(lambda: b"c" in bytes(self.ring.written), 3.0))
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pdr_step_length"], 0.93)

        status, loop = _post(self.port, "/api/pdr/start", {"kind": "swing"})
        self.assertEqual(status, 200, loop)
        self.assertTrue(wait_until(lambda: _get(self.port, "/api/state")[1]["pdr_test"]["phase"] == "walk", 3.0))
        status, first = _post(self.port, "/api/pdr/end", {})
        self.assertEqual(first["marks"], 1)
        status, second = _post(self.port, "/api/pdr/end", {})
        self.assertEqual(status, 200, second)
        self.assertEqual(second["closure_error_m"], 0.0)
        self.assertEqual(second["phase"], "done")
        self.assertTrue(second["camera_paused"])
        self.assertEqual(len(list(self.reports.glob("pdr_loop_swing_*/summary.json"))), 1)
        self.assertEqual(self.loads, [])
        self.assertEqual(REAL_CONFIG.read_bytes(), self.real)


if __name__ == "__main__":
    unittest.main()
