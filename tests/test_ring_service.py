"""The web service over real HTTP and WebSocket, with a simulated ring behind it."""

from __future__ import annotations

import asyncio
import contextlib
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
from pathlib import Path

import uvicorn
import websockets

from ring.config import Config
from ring.service import build, create_app
from tests.ring_sim import SimRing, World, covered, wait_until


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world = World(41)
        cls.ring = SimRing(cls.world.view(0))
        cls.folder = Path(tempfile.mkdtemp(prefix="retrace-web-"))
        cls.port = free_port()
        config = Config(
            save_capture_s=0.5,
            save_lookback_s=1.0,
            reconnect_seconds=0.1,
            stall_seconds=1.0,
            embedder_enabled=False,
        )
        cls.config = config
        store, link, engine = build(cls.folder, config, opener=cls.ring.opener)
        cls.engine = engine
        cls.server = uvicorn.Server(
            uvicorn.Config(create_app(store, link, engine, cls.port), host="127.0.0.1", port=cls.port, log_level="error")
        )
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        assert wait_until(lambda: cls.server.started, 10.0)
        assert wait_until(lambda: engine.snapshot()["frames"]["kept"] >= 1, 10.0), engine.snapshot()["frames"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        shutil.rmtree(cls.folder, ignore_errors=True)

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def post(self, path: str, body: dict | None = None, **headers):
        return self.call("POST", path, body or {}, {"X-ReTrace": "1", **headers})

    def test_1_dashboard_page_and_state(self) -> None:
        status, body, headers = self.call("GET", "/")
        self.assertEqual(status, 200)
        text = body.decode()
        for label in (
            "Save Item",
            "Saved Items",
            "Tracking",
            "Motor feedback",
            "Mark put-down",
            "Breadcrumb timeline",
            "not a map",
        ):
            self.assertIn(label, text)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.call("GET", "/static/app.js")[0], 200)
        status, body, _ = self.call("GET", "/api/state")
        state = json.loads(body)
        for key in ("link", "frames", "memory", "items", "find", "motor", "mapper", "notices", "capture", "save", "mode"):
            self.assertIn(key, state)
        self.assertEqual(state["mode"]["memory"], "CONTINUOUS MEMORY ACTIVE")
        self.assertTrue(state["link"]["connected"])
        self.assertTrue(state["link"]["imu"]["ok"])
        self.assertEqual(state["mapper"]["state"], "off")
        self.assertFalse(state["mapper"]["reliable"])

    def test_2_requests_from_other_pages_or_other_host_names_are_refused(self) -> None:
        self.assertEqual(self.call("POST", "/api/items", {"name": "Keys"})[0], 403)  # no dashboard header
        self.assertEqual(self.post("/api/items", {"name": "Keys"}, Origin="http://evil.example")[0], 403)
        self.assertEqual(self.post("/api/motor/test", Origin="http://evil.example")[0], 403)
        self.assertEqual(self.call("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.engine.snapshot()["items"], [])
        self.assertEqual(bytes(self.ring.written), b"")

    def test_3_save_find_stop_delete_through_the_api(self) -> None:
        status, body, _ = self.post("/api/items", {"name": ""})
        self.assertEqual(status, 409)
        self.assertIn("name", json.loads(body)["detail"])
        status, body, _ = self.post("/api/items", {"name": "Keys"}, Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200, body)
        item_id = json.loads(body)["item_id"]
        self.assertTrue(wait_until(lambda: self.engine.snapshot()["items"][0]["trail_state"] == "recording", 4.0))
        status, body, _ = self.call("GET", f"/api/items/{item_id}")
        detail = json.loads(body)
        self.assertEqual(detail["name"], "Keys")
        status, body, headers = self.call("GET", f"/api/keyframes/{detail['references'][0]}.jpg")
        self.assertEqual((status, body[:2], headers["Content-Type"]), (200, b"\xff\xd8", "image/jpeg"))
        self.assertEqual(self.call("GET", "/api/keyframes/999999.jpg")[0], 404)

        self.assertEqual(self.post(f"/api/items/{item_id}/find")[0], 200)
        self.assertTrue(wait_until(lambda: self.engine.snapshot()["find"] is not None, 3.0))
        self.assertEqual(self.engine.snapshot()["find"]["name"], "Keys")
        self.assertEqual(self.post("/api/find/stop")[0], 200)
        self.assertTrue(wait_until(lambda: self.engine.snapshot()["find"] is None, 3.0))
        self.assertEqual(self.post(f"/api/items/{item_id}/delete")[0], 200)
        self.assertEqual(self.post(f"/api/items/{item_id}/find")[0], 409)

    def test_4_vibration_switch_and_test_button(self) -> None:
        before = len(self.ring.written)
        self.assertEqual(self.post("/api/motor/enabled", {"enabled": False})[0], 200)
        status, body, _ = self.post("/api/motor/test")
        self.assertEqual(json.loads(body), {"sent": False, "blocked": "vibration is switched off"})
        self.assertEqual(self.post("/api/motor/enabled", {"enabled": True})[0], 200)
        status, body, _ = self.post("/api/motor/test")
        self.assertTrue(json.loads(body)["sent"])
        status, body, _ = self.post("/api/motor/test")
        self.assertFalse(json.loads(body)["sent"])
        self.assertTrue(wait_until(lambda: len(self.ring.written) == before + 1, 3.0))
        self.assertEqual(bytes(self.ring.written[before:]), b"v")

    def test_5_live_socket_sends_pictures_and_state_and_refuses_other_origins(self) -> None:
        async def session(origin: str):
            pictures, states = 0, []
            async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws", origin=origin) as socket_:
                for _ in range(12):
                    message = await asyncio.wait_for(socket_.recv(), timeout=5)
                    if isinstance(message, bytes):
                        self.assertEqual(message[:2], b"\xff\xd8")
                        pictures += 1
                    else:
                        states.append(json.loads(message))
            return pictures, states

        pictures, states = asyncio.run(session(f"http://127.0.0.1:{self.port}"))
        self.assertGreaterEqual(pictures, 5)
        self.assertTrue(states and states[0]["link"]["connected"])
        with self.assertRaises(Exception):
            asyncio.run(session("http://evil.example"))

    def test_6_save_with_a_covered_lens_still_persists_the_item(self) -> None:
        state = self.engine.snapshot
        self.ring.set_picture(covered(self.world.view(0), 1.0))
        self.assertTrue(
            wait_until(
                lambda: state()["frames"]["last"].get("verdict") in ("obstructed", "observed", "covered", "few_features"),
                4.0,
            ),
            state()["frames"],
        )
        time.sleep(self.config.save_lookback_s + 0.2)

        status, body, _ = self.post("/api/items", {"name": "Keys"})
        answer = json.loads(body)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["state"], "capturing")
        self.assertIsNotNone(answer["item_id"])
        self.assertEqual([item["id"] for item in state()["items"]], [answer["item_id"]])
        self.assertIn(answer.get("anchor_quality"), ("missing", "limited", "ready"))
        self.assertTrue(wait_until(lambda: state()["save"]["state"] == "saved", 4.0), state()["save"])
        self.assertEqual(self.post(f"/api/items/{answer['item_id']}/delete")[0], 200)

    def test_7_requests_that_cannot_be_understood_get_a_reason(self) -> None:
        status, body, _ = self.post("/api/items", {"name": ["Keys"]})
        self.assertEqual((status, json.loads(body)["code"]), (422, "invalid_name"))
        status, body, _ = self.post("/api/items", {"name": "x" * 60})
        self.assertEqual((status, json.loads(body)["code"]), (409, "invalid_name"))
        status, body, _ = self.post("/api/items/999/find")
        self.assertEqual(status, 409)
        self.assertIsInstance(json.loads(body)["detail"], str)

    def test_8_an_unexpected_failure_reaches_the_dashboard_as_text(self) -> None:
        def broken(_item_id: int):
            raise OSError(5, "simulated disk failure")

        original = self.engine.item_detail
        self.engine.item_detail = broken
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                status, body, _ = self.call("GET", "/api/items/1")
        finally:
            self.engine.item_detail = original
        answer = json.loads(body)
        self.assertEqual((status, answer["code"]), (500, "storage_error"))
        self.assertIn("Filesystem error", answer["detail"])


if __name__ == "__main__":
    unittest.main()
