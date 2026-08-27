"""Voice WebSocket session-identity binding regressions.

The HTTP surface binds a browser session to the Tailscale identity that
created it. These tests pin the equivalent boundary on `/ws/voice`, which
previously validated only Origin, cookie, and CSRF, so a session cookie
copied to a second tailnet identity still reached the recognizer.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
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
        app, service, _settings = build_app(
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
                assert service.sessions.require(_session_id(http)).messages == []
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


def test_direct_same_socket_peer_session_remains_supported(tmp_path: Path) -> None:
    """The fallback equivalence class is intentionally the direct socket peer."""

    async def scenario() -> None:
        app, service, _settings = build_app(
            tmp_path, stt_engine_factory=TranscribingEngine
        )
        try:
            session = service.sessions.create(peer_key="peer:127.0.0.1")
            socket = WebSocketHarness(
                app,
                "/ws/voice",
                headers=_socket_headers(session.session_id, None),
            )
            await socket.connect()
            await _start_frame(socket, session.csrf_token)
            assert (await socket.receive())["type"] == "listening"
            await socket.disconnect()
            await socket.finish()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_missing_session_is_rejected_before_accept_and_stt(tmp_path: Path) -> None:
    async def scenario() -> None:
        CountingEngine.created = 0
        app, _service, _settings = build_app(
            tmp_path, stt_engine_factory=CountingEngine
        )
        try:
            socket = WebSocketHarness(
                app,
                "/ws/voice",
                headers=_socket_headers("A" * 43, OWNER),
            )
            await socket.incoming.put({"type": "websocket.connect"})
            assert (await _first_frame(socket))["kind"] == "websocket.close"
            await socket.finish()
            assert CountingEngine.created == 0
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize("login", ["   ", "intruder@example.com"])
def test_voice_socket_rejects_missing_or_mismatched_identity_before_stt(
    tmp_path: Path, login: str
) -> None:
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
                    headers=_socket_headers(_session_id(http), login),
                )
                await socket.incoming.put({"type": "websocket.connect"})
                assert (await _first_frame(socket))["kind"] == "websocket.close"
                await socket.finish()
                assert CountingEngine.created == 0
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_untrusted_proxy_identity_cannot_reuse_a_bound_session(tmp_path: Path) -> None:
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
                    headers=_socket_headers(_session_id(http), OWNER),
                    client_host="100.64.0.22",
                )
                await socket.incoming.put({"type": "websocket.connect"})
                assert (await _first_frame(socket))["kind"] == "websocket.close"
                await socket.finish()
                assert CountingEngine.created == 0
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize("csrf", [None, "wrong-token"])
def test_missing_or_invalid_csrf_is_rejected_before_stt(
    tmp_path: Path, csrf: str | None
) -> None:
    async def scenario() -> None:
        CountingEngine.created = 0
        app, _service, _settings = build_app(
            tmp_path, stt_engine_factory=CountingEngine
        )
        try:
            async with client(app) as http:
                session = await start_session(http, headers=_identity(OWNER))
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(_session_id(http), OWNER),
                )
                await socket.connect()
                await _start_frame(socket, csrf or "")
                event = await socket.receive()
                assert event["type"] == "error"
                assert event["code"] == "csrf_denied"
                assert CountingEngine.created == 0
                assert session["csrf_token"] != csrf
                await socket.finish()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_invalid_origin_and_expired_session_are_rejected_before_accept(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        CountingEngine.created = 0
        app, service, _settings = build_app(
            tmp_path, stt_engine_factory=CountingEngine
        )
        try:
            async with client(app) as http:
                await start_session(http, headers=_identity(OWNER))
                session_id = _session_id(http)
                bad_origin = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers={
                        **_socket_headers(session_id, OWNER),
                        "origin": "http://attacker.invalid",
                    },
                )
                await bad_origin.incoming.put({"type": "websocket.connect"})
                assert (await _first_frame(bad_origin))["kind"] == "websocket.close"
                await bad_origin.finish()

                session = service.sessions.require(session_id)
                session.last_active_monotonic = (
                    time.monotonic() - service.sessions.ttl_seconds - 1
                )
                expired = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(session_id, OWNER),
                )
                await expired.incoming.put({"type": "websocket.connect"})
                assert (await _first_frame(expired))["kind"] == "websocket.close"
                await expired.finish()
                assert CountingEngine.created == 0
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_cookie_and_csrf_from_different_sessions_are_rejected_before_stt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        CountingEngine.created = 0
        app, service, _settings = build_app(
            tmp_path, stt_engine_factory=CountingEngine
        )
        try:
            first = service.sessions.create(peer_key="identity:" + OWNER)
            second = service.sessions.create(peer_key="identity:" + OWNER)
            socket = WebSocketHarness(
                app,
                "/ws/voice",
                headers=_socket_headers(first.session_id, OWNER),
            )
            await socket.connect()
            await _start_frame(socket, second.csrf_token)
            event = await socket.receive()
            assert event["type"] == "error"
            assert event["code"] == "csrf_denied"
            assert first.messages == second.messages == []
            assert CountingEngine.created == 0
            await socket.finish()
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_a_disconnect_during_routing_unwinds_the_cancel_watch_cleanly(
    tmp_path: Path,
) -> None:
    """The socket watches for frames while routing runs, so a late cancel can
    be observed. That concurrent watch must unwind cleanly when the browser
    leaves mid-turn: the routing thread is still awaited, the frame listener is
    cancelled rather than left pending on a closed socket, and the voice slot
    and recognizer go back to their pools intact for the next turn.
    """

    async def scenario() -> None:
        app, service, _settings = build_app(
            tmp_path, stt_engine_factory=TranscribingEngine
        )
        routing_started = threading.Event()
        may_finish = threading.Event()
        original = service.handle_text

        def slow_handle_text(*args, **kwargs):
            routing_started.set()
            may_finish.wait(5)
            return original(*args, **kwargs)

        service.handle_text = slow_handle_text
        try:
            async with client(app) as http:
                session = await start_session(http, headers=_identity(OWNER))
                socket = WebSocketHarness(
                    app,
                    "/ws/voice",
                    headers=_socket_headers(_session_id(http), OWNER),
                )
                await socket.connect()
                await _start_frame(socket, str(session["csrf_token"]))
                assert (await socket.receive())["type"] == "listening"
                await socket.send_bytes(
                    (1000).to_bytes(2, "little", signed=True) * 640
                )
                await socket.send_json({"type": "stop", "endpoint_reason": "tap"})
                # Leave while the turn is provably still being routed.
                deadline = asyncio.get_running_loop().time() + 5
                while not routing_started.is_set():
                    assert asyncio.get_running_loop().time() < deadline
                    await asyncio.sleep(0.005)
                await socket.disconnect()
                await asyncio.sleep(0.05)
                may_finish.set()
                await socket.finish()
                # The engine must go back to the pool intact and reusable. One
                # retired as unreusable is closed instead, leaving nothing
                # available and dropping the created count.
                stats = app.state.stt_pool.stats()
                assert stats["in_use"] == 0
                assert stats["available"] == 1, stats
                assert stats["created"] == 1, stats
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())
