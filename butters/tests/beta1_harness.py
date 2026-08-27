"""Shared Beta 1 web harness for adversarial regression tests."""

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


PRODUCTION_ORIGIN = "https://butters.example-tailnet.ts.net"
ADMIN_IDENTITY = "admin@example.com"


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-12T12:00:00Z",
            (SensorRecord("environment", "3", "2026-08-12T12:00:00Z", 1, "online", {"humidity": 42.0}),),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class NoCloud:
    available = False


class TranscribingEngine:
    """Minimal streaming recognizer with a controllable teardown failure."""

    initialization_seconds = 0.0

    def __init__(self, *, fail_close: bool = False) -> None:
        self.fail_close = fail_close
        self.closed = False

    def start_utterance(self) -> None:
        return None

    def accept_audio(self, _frame):
        return "what is the humidity in box three"

    def get_partial_transcript(self) -> str:
        return "what is the humidity in box three"

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        return "what is the humidity in box three"

    def reset(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("native recognizer teardown failed")


def build_settings(tmp_path: Path, **web: object):
    base = load_assistant_settings()
    defaults: dict[str, object] = {
        "state_dir": tmp_path,
        "development_mode": True,
        "admin_identities": (ADMIN_IDENTITY,),
    }
    defaults.update(web)
    return replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, **defaults).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )


def build_app(tmp_path: Path, *, stt_engine_factory=None, **web: object):
    settings = build_settings(tmp_path, **web)
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=NoCloud(),
        state_dir=tmp_path,
    )
    app = create_app(settings, vocabulary, service, stt_engine_factory=stt_engine_factory)
    return app, service, settings


def client(app, *, base_url: str = "http://testserver") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url=base_url)


def admin_headers(origin: str | None = None, csrf: str | None = None) -> dict[str, str]:
    headers = {"tailscale-user-login": ADMIN_IDENTITY}
    if origin:
        headers["origin"] = origin
    if csrf:
        headers["x-butters-csrf"] = csrf
    return headers


def peer_identity_headers(peer_key: str) -> dict[str, str]:
    """Present the tailnet identity a session is bound to.

    `/ws/voice` binds a session to its creating identity exactly as the HTTP
    surface does, so a test that fabricates a session under `identity:...`
    must connect as that identity, like the browser it stands in for.
    """

    return (
        {"tailscale-user-login": peer_key.removeprefix("identity:")}
        if peer_key.startswith("identity:")
        else {}
    )


async def start_session(
    http: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    response = await http.get("/api/session", headers=headers or {})
    response.raise_for_status()
    return response.json()


class WebSocketHarness:
    """Drive an ASGI WebSocket route directly, as a real loopback peer would."""

    def __init__(
        self,
        app,
        path: str,
        *,
        headers: dict[str, str],
        host: str = "testserver",
        client_host: str = "127.0.0.1",
    ) -> None:
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.outgoing: asyncio.Queue = asyncio.Queue()
        request_headers = {"host": host, **headers}
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.5"},
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
            "client": (client_host, 1234),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
            "extensions": {},
        }
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.outgoing.put))

    async def connect(self) -> None:
        await self.incoming.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self.outgoing.get(), 2)
        assert message["type"] == "websocket.accept", message

    async def send_json(self, value: object) -> None:
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def send_bytes(self, value: bytes) -> None:
        await self.incoming.put({"type": "websocket.receive", "bytes": value})

    async def receive(self, timeout: float = 3.0):
        message = await asyncio.wait_for(self.outgoing.get(), timeout)
        if message["type"] == "websocket.send":
            return json.loads(message["text"])
        return message

    async def disconnect(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1001})

    async def finish(self, timeout: float = 5.0) -> None:
        try:
            await asyncio.wait_for(self.task, timeout)
        except (asyncio.TimeoutError, Exception):
            if not self.task.done():
                self.task.cancel()
