from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx

from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import SensorRecord, SensorSnapshot, ServerHealthSnapshot
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService


class Sensors:
    def snapshot(self):
        return SensorSnapshot("now", (SensorRecord("environment", "3", "now", 1, "online", {"humidity": 42.0}),))


class Health:
    def snapshot(self):
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class General:
    available = False


class Engine:
    initialization_seconds = 0.0
    def start_utterance(self): pass
    def accept_audio(self, _frame): return "what is the humidity in box three"
    def get_partial_transcript(self): return "what is the humidity in box three"
    def endpoint_detected(self): return False
    def finalize(self): return "what is the humidity in box three"
    def reset(self): pass
    def close(self): pass


def _app(tmp_path: Path):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True, admin_identities=("admin@example.com",)),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health())
    service = BetaAssistantService(settings, vocabulary, assistant=assistant, general_reasoner=General(), state_dir=tmp_path)
    return create_app(settings, vocabulary, service, stt_engine_factory=Engine), service


async def _http(app, path: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: dict[str, object] | None = None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.request(method, path, headers=headers, json=body)
    return response.status_code, dict(response.headers), response.content


async def _session(app):
    status, headers, body = await _http(app, "/api/session")
    assert status == 200
    cookie = headers["set-cookie"].split(";", 1)[0]
    return json.loads(body), cookie


class WebSocketHarness:
    def __init__(self, app, path: str, *, headers: dict[str, str]) -> None:
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        request_headers = {"host": "testserver", **headers}
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
            "extensions": {},
        }
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.outgoing.put))

    async def connect(self):
        await self.incoming.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self.outgoing.get(), 2)
        assert message["type"] == "websocket.accept"

    async def send_json(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def send_text(self, value: str):
        await self.incoming.put({"type": "websocket.receive", "text": value})

    async def send_bytes(self, value: bytes):
        await self.incoming.put({"type": "websocket.receive", "bytes": value})

    async def receive(self):
        message = await asyncio.wait_for(self.outgoing.get(), 3)
        if message["type"] == "websocket.send":
            return json.loads(message["text"])
        return message

    async def close(self):
        if not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 3)


def test_normal_text_request_and_admin_authorization(tmp_path: Path) -> None:
    async def scenario():
        app, _service = _app(tmp_path)
        try:
            session, cookie = await _session(app)
            headers = {"origin": "http://testserver", "x-butters-csrf": session["csrf_token"], "cookie": cookie}
            response = await _http(app, "/api/chat", method="POST", headers=headers, body={"text": "what is the humidity in box three"})
            denied = await _http(app, "/api/admin/overview", headers={"cookie": cookie})
            allowed = await _http(app, "/api/admin/overview", headers={"cookie": cookie, "tailscale-user-login": "admin@example.com"})
            payload = json.loads(response[2])
            assert response[0] == 200 and payload["route"] == "deterministic" and "42" in payload["response_text"]
            assert denied[0] == 403
            assert allowed[0] == 200
        finally:
            await app.state.shutdown_workers()
    asyncio.run(scenario())


def test_credential_endpoint_never_returns_key(tmp_path: Path, monkeypatch) -> None:
    async def scenario():
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-parent-secret-value")
        app, _service = _app(tmp_path)
        try:
            response = await _http(app, "/api/admin/security", headers={"tailscale-user-login": "admin@example.com"})
            payload = json.loads(response[2])
            assert response[0] == 200
            assert payload["credentials"]["openai"]["configured"] is True
            assert b"sk-fake" not in response[2] and b"secret-value" not in response[2]
        finally:
            await app.state.shutdown_workers()
    asyncio.run(scenario())


def test_websocket_protocol_rejects_malformed_start(tmp_path: Path) -> None:
    async def scenario():
        app, _service = _app(tmp_path)
        try:
            _session_data, cookie = await _session(app)
            socket = WebSocketHarness(app, "/ws/voice", headers={"origin": "http://testserver", "cookie": cookie})
            await socket.connect()
            await socket.send_text("not-json")
            response = await socket.receive()
            assert response["type"] == "error" and response["code"] == "protocol_error"
            await socket.close()
        finally:
            await app.state.shutdown_workers()
    asyncio.run(scenario())


def test_voice_final_uses_same_assistant_path(tmp_path: Path) -> None:
    async def scenario():
        app, _service = _app(tmp_path)
        try:
            session, cookie = await _session(app)
            socket = WebSocketHarness(app, "/ws/voice", headers={"origin": "http://testserver", "cookie": cookie})
            await socket.connect()
            await socket.send_json({"type": "start", "csrf_token": session["csrf_token"], "sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le"})
            assert (await socket.receive())["type"] == "listening"
            await socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 640)
            received = [await socket.receive(), await socket.receive()]
            assert {item["type"] for item in received} == {"speech_start", "partial"}
            await socket.send_json({"type": "stop"})
            final = await socket.receive()
            assistant = await socket.receive()
            assert final["type"] == "final"
            assert assistant["type"] == "assistant" and assistant["route"] == "deterministic" and "42" in assistant["response_text"]
            await socket.close()
        finally:
            await app.state.shutdown_workers()
    asyncio.run(scenario())
