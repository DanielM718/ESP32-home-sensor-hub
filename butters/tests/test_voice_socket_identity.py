"""Voice WebSocket session-identity binding regressions.

The HTTP surface binds a browser session to the Tailscale identity that
created it. These tests pin the equivalent boundary on `/ws/voice`, which
previously validated only Origin, cookie, and CSRF, so a session cookie
copied to a second tailnet identity still reached the recognizer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from beta1_harness import (
    TranscribingEngine,
    WebSocketHarness,
    build_app,
    client,
    start_session,
)

OWNER = "owner@example.com"
INTRUDER = "intruder@example.com"


class CountingEngine(TranscribingEngine):
    """Records every recognizer allocation the route performs."""

    created = 0

    def __init__(self) -> None:
        super().__init__()
        type(self).created += 1


def _identity(login: str) -> dict[str, str]:
    return {"tailscale-user-login": login}


def _socket_headers(session_id: str, login: str | None) -> dict[str, str]:
    headers = {
        "origin": "http://testserver",
        "cookie": f"butters_session={session_id}",
    }
    if login is not None:
        headers["tailscale-user-login"] = login
    return headers


def _session_id(http) -> str:
    value = http.cookies.get("butters_session")
    assert value, "session cookie was not issued"
    return str(value)


async def _start_frame(socket: WebSocketHarness, csrf: str) -> None:
    await socket.send_json(
        {
            "type": "start",
            "csrf_token": csrf,
            "sample_rate": 16000,
            "channels": 1,
            "encoding": "pcm_s16le",
        }
    )


async def _first_frame(socket: WebSocketHarness) -> dict[str, object]:
    """Return the first ASGI frame the route produced, accepted or refused."""

    message = await asyncio.wait_for(socket.outgoing.get(), 3)
    if message["type"] == "websocket.send":
        return {"kind": "send", "payload": json.loads(message["text"])}
    return {"kind": message["type"], "payload": message}


def test_voice_socket_accepts_the_session_owning_peer(tmp_path: Path) -> None:
    """The legitimate owner must still reach the recognizer."""

    async def scenario() -> None:
        app, _service, _settings = build_app(
            tmp_path, stt_engine_factory=TranscribingEngine
        )
        try:
            async with client(app) as http:
                session = await start_session(http, headers=_identity(OWNER))
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(_session_id(http), OWNER),
                )
                try:
                    await socket.connect()
                    await _start_frame(socket, str(session["csrf_token"]))
                    event = await socket.receive()
                    assert event["type"] == "listening", event
                finally:
                    await socket.disconnect()
                    await socket.finish()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_voice_socket_rejects_a_session_copied_to_another_peer(tmp_path: Path) -> None:
    """The confirmed probe: cookie + CSRF replayed from a second identity."""

    async def scenario() -> None:
        CountingEngine.created = 0
        app, _service, _settings = build_app(
            tmp_path, stt_engine_factory=CountingEngine
        )
        try:
            async with client(app) as http:
                await start_session(http, headers=_identity(OWNER))
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(_session_id(http), INTRUDER),
                )
                try:
                    await socket.incoming.put({"type": "websocket.connect"})
                    outcome = await _first_frame(socket)
                    # The route must refuse before it accepts the handshake, so
                    # no listening or cancelled event can ever be observed.
                    assert outcome["kind"] == "websocket.close", outcome
                finally:
                    await socket.finish()
                # Rejection precedes every expensive resource: no recognizer was
                # allocated for the intruder's connection.
                assert CountingEngine.created == 0
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_voice_socket_rejects_a_session_when_identity_is_absent(tmp_path: Path) -> None:
    """A caller presenting no tailnet identity may not use a bound session."""

    async def scenario() -> None:
        app, _service, _settings = build_app(
            tmp_path, stt_engine_factory=TranscribingEngine
        )
        try:
            async with client(app) as http:
                await start_session(http, headers=_identity(OWNER))
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(_session_id(http), None),
                )
                try:
                    await socket.incoming.put({"type": "websocket.connect"})
                    outcome = await _first_frame(socket)
                    assert outcome["kind"] == "websocket.close", outcome
                finally:
                    await socket.finish()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())
