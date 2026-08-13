"""Socket-level regressions for the production Butters Uvicorn boundary."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import uvicorn
import websockets
from beta1_harness import (
    ADMIN_IDENTITY,
    PRODUCTION_ORIGIN,
    TranscribingEngine,
    build_app,
    build_settings,
)
from butters.web import __main__ as server_entrypoint

TAILNET_CLIENT = "100.101.102.103"
SERVE_HEADERS = {
    "X-Forwarded-For": TAILNET_CLIENT,
    "X-Forwarded-Proto": "https",
    "Tailscale-User-Login": ADMIN_IDENTITY,
}


class ScopeRecorder:
    """Record the scope delivered by Uvicorn to the application."""

    def __init__(self, application: Any) -> None:
        self.application = application
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            self.scopes.append(
                {
                    "type": scope["type"],
                    "path": scope["path"],
                    "client": scope.get("client"),
                    "scheme": scope.get("scheme"),
                }
            )
        await self.application(scope, receive, send)

    def latest(self, kind: str, path: str) -> dict[str, Any]:
        return next(
            scope
            for scope in reversed(self.scopes)
            if scope["type"] == kind and scope["path"] == path
        )


def production_config(settings: Any, application: Any) -> uvicorn.Config:
    """Capture the options passed by the real service entry point."""

    with (
        patch.object(server_entrypoint, "load_assistant_settings", return_value=settings),
        patch.object(server_entrypoint.uvicorn, "run") as run,
    ):
        server_entrypoint.main()
    run.assert_called_once()
    positional, options = run.call_args
    assert positional == ("butters.web.app:create_app",)
    assert options["factory"] is True
    return uvicorn.Config(application, **{**options, "factory": False})


@asynccontextmanager
async def running_server(
    tmp_path: Path,
    *,
    proxy_headers: bool | None = None,
) -> AsyncIterator[tuple[str, ScopeRecorder, Any]]:
    assert str(tmp_path).startswith("/tmp/")
    app, service, settings = build_app(
        tmp_path,
        development_mode=False,
        allowed_origins=(PRODUCTION_ORIGIN,),
        stt_engine_factory=TranscribingEngine,
    )
    recorder = ScopeRecorder(app)
    config = production_config(settings, recorder)
    if proxy_headers is not None:
        config.proxy_headers = proxy_headers

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(200):
            if server.started:
                break
            if task.done():
                task.result()
            await asyncio.sleep(0.01)
        assert server.started
        yield f"127.0.0.1:{port}", recorder, service
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            listener.close()


def test_production_server_configuration_disables_proxy_rewriting(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    config = production_config(settings, object())

    assert config.host == "127.0.0.1"
    assert config.proxy_headers is False


def test_serve_headers_preserve_peer_and_http_security_boundaries(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as (authority, recorder, service):
            base_url = f"http://{authority}"
            async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
                allowed = await client.get(
                    "/admin",
                    headers={**SERVE_HEADERS, "Origin": PRODUCTION_ORIGIN},
                )
                assert allowed.status_code == 200
                admin_scope = recorder.latest("http", "/admin")
                assert admin_scope["client"][0] == "127.0.0.1"
                assert admin_scope["client"][1] > 0
                assert admin_scope["scheme"] == "http"

                wrong = await client.get(
                    "/admin",
                    headers={**SERVE_HEADERS, "Tailscale-User-Login": "intruder@example.com"},
                )
                assert wrong.status_code == 403
                assert wrong.json()["error"] == "admin_identity_denied"

                missing = await client.get(
                    "/admin",
                    headers={
                        "X-Forwarded-For": TAILNET_CLIENT,
                        "X-Forwarded-Proto": "https",
                    },
                )
                assert missing.status_code == 403
                assert missing.json()["error"] == "admin_identity_missing"

                session_response = await client.get(
                    "/api/session",
                    headers={**SERVE_HEADERS, "Origin": PRODUCTION_ORIGIN},
                )
                assert session_response.status_code == 200
                cookie = session_response.headers["set-cookie"]
                assert "Secure" in cookie
                assert "HttpOnly" in cookie
                assert "SameSite=strict" in cookie
                session_payload = session_response.json()
                session_id = session_response.cookies["butters_session"]
                session = service.sessions.require(session_id)
                assert session.peer_key == f"identity:{ADMIN_IDENTITY}"
                assert session.administrator is True

                mutation_headers = {
                    **SERVE_HEADERS,
                    "Origin": PRODUCTION_ORIGIN,
                    "X-Butters-CSRF": session_payload["csrf_token"],
                    "Cookie": f"butters_session={session_id}",
                }
                accepted = await client.post(
                    "/api/chat",
                    headers=mutation_headers,
                    json={"text": "what is the humidity in box three"},
                )
                assert accepted.status_code == 200
                assert accepted.json()["route"] == "deterministic"

                rejected_origins = (
                    (None, "origin_missing"),
                    ("https://attacker.example", "origin_denied"),
                    ("http://butters.example-tailnet.ts.net", "origin_denied"),
                )
                for origin, expected_error in rejected_origins:
                    headers = {key: value for key, value in mutation_headers.items() if key != "Origin"}
                    if origin is not None:
                        headers["Origin"] = origin
                    rejected = await client.post(
                        "/api/chat",
                        headers=headers,
                        json={"text": "what is the humidity in box three"},
                    )
                    assert rejected.status_code == 403
                    assert rejected.json()["error"] == expected_error

                for csrf in (None, "incorrect-token"):
                    headers = {
                        key: value
                        for key, value in mutation_headers.items()
                        if key != "X-Butters-CSRF"
                    }
                    if csrf is not None:
                        headers["X-Butters-CSRF"] = csrf
                    rejected = await client.post(
                        "/api/chat",
                        headers=headers,
                        json={"text": "what is the humidity in box three"},
                    )
                    assert rejected.status_code == 403
                    assert rejected.json()["error"] == "csrf_denied"

    asyncio.run(scenario())


def test_serve_headers_preserve_voice_and_admin_websockets(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path) as (authority, recorder, _service):
            async with httpx.AsyncClient(
                base_url=f"http://{authority}",
                trust_env=False,
            ) as client:
                session_response = await client.get(
                    "/api/session",
                    headers={**SERVE_HEADERS, "Origin": PRODUCTION_ORIGIN},
                )
            session_payload = session_response.json()
            session_id = session_response.cookies["butters_session"]
            websocket_headers = {
                **SERVE_HEADERS,
                "Cookie": f"butters_session={session_id}",
            }

            async with websockets.connect(
                f"ws://{authority}/ws/voice",
                origin=PRODUCTION_ORIGIN,
                additional_headers=websocket_headers,
                proxy=None,
            ) as voice:
                await voice.send(
                    json.dumps(
                        {
                            "type": "start",
                            "csrf_token": session_payload["csrf_token"],
                            "sample_rate": 16000,
                            "channels": 1,
                            "encoding": "pcm_s16le",
                        }
                    )
                )
                assert json.loads(await voice.recv())["type"] == "listening"
                await voice.send((1000).to_bytes(2, "little", signed=True) * 640)
                first = json.loads(await voice.recv())
                second = json.loads(await voice.recv())
                assert {first["type"], second["type"]} == {"speech_start", "partial"}
                await voice.send(json.dumps({"type": "stop"}))
                final = json.loads(await voice.recv())
                assistant = json.loads(await voice.recv())
                assert final["type"] == "final"
                assert assistant["type"] == "assistant"
                assert assistant["route"] == "deterministic"

            voice_scope = recorder.latest("websocket", "/ws/voice")
            assert voice_scope["client"][0] == "127.0.0.1"
            assert voice_scope["scheme"] == "ws"

            async with websockets.connect(
                f"ws://{authority}/ws/admin/traces",
                origin=PRODUCTION_ORIGIN,
                additional_headers=SERVE_HEADERS,
                proxy=None,
            ) as traces:
                message = json.loads(await traces.recv())
                assert message["type"] == "traces"

            trace_scope = recorder.latest("websocket", "/ws/admin/traces")
            assert trace_scope["client"][0] == "127.0.0.1"
            assert trace_scope["scheme"] == "ws"

    asyncio.run(scenario())


def test_non_loopback_asgi_peer_cannot_forge_proxy_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(
            tmp_path,
            development_mode=False,
            allowed_origins=(PRODUCTION_ORIGIN,),
        )
        transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=False,
            client=(TAILNET_CLIENT, 4242),
        )
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url=PRODUCTION_ORIGIN,
            ) as client:
                response = await client.get(
                    "/admin",
                    headers={
                        **SERVE_HEADERS,
                        "X-Forwarded-For": "127.0.0.1",
                        "Origin": PRODUCTION_ORIGIN,
                    },
                )
                assert response.status_code == 403
                assert response.json()["error"] == "untrusted_proxy_peer"
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_previous_uvicorn_proxy_default_reproduces_authorization_failure(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with running_server(tmp_path, proxy_headers=True) as (authority, recorder, _service):
            async with httpx.AsyncClient(
                base_url=f"http://{authority}",
                trust_env=False,
            ) as client:
                response = await client.get("/admin", headers=SERVE_HEADERS)

            assert response.status_code == 403
            assert response.json()["error"] == "untrusted_proxy_peer"
            scope = recorder.latest("http", "/admin")
            assert scope["client"] == (TAILNET_CLIENT, 0)
            assert scope["scheme"] == "https"

    asyncio.run(scenario())
