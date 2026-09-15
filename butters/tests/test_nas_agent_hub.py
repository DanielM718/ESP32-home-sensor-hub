"""NAS Agent hub authentication, state, and request-boundary tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
from butters.actions.nas_agent import NasAgentHub
from butters.actions.nas_protocol import (
    SCHEMAS,
    canonical,
    decode,
    envelope,
    sign,
    verify,
)
from butters.assistant_config import NasAgentIngressSettings

TOKEN = "n" * 64
KEY = b"z" * 32


class Socket:
    def __init__(self, *incoming: str) -> None:
        self.headers: dict[str, str] = {}
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        for item in incoming:
            self.incoming.put_nowait(item)
        self.sent: list[str] = []
        self.sent_event = asyncio.Event()
        self.accepted = False
        self.closed: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise ConnectionError("closed")
        return value

    async def send_text(self, value: str) -> None:
        self.sent.append(value)
        self.sent_event.set()

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


class Clock:
    def __init__(self) -> None:
        self.monotonic_now = 100.0
        self.wall_now = 1_700_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_now

    def wall(self) -> float:
        return self.wall_now

    def advance(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += seconds


def _credentials(tmp_path: Path) -> Path:
    key = tmp_path / "nas-command.key"
    key.write_text(KEY.hex(), encoding="utf-8")
    key.chmod(0o640)
    config = tmp_path / "nas-agent.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                "protocol_version = 1",
                'agent_id = "nas-primary"',
                f'token_sha256 = "{hashlib.sha256(TOKEN.encode()).hexdigest()}"',
                f'command_key_file = "{key}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    config.chmod(0o640)
    return config


def _hub(
    tmp_path: Path, *, clock: Clock | None = None, timeout: int = 2
) -> NasAgentHub:
    clock = clock or Clock()
    settings = NasAgentIngressSettings(
        enabled=True,
        config_path=_credentials(tmp_path),
        request_timeout_seconds=timeout,
    ).validated()
    return NasAgentHub(
        settings,
        monotonic=clock.monotonic,
        wall_clock=clock.wall,
    )


def _hello(**changes: object) -> str:
    value: dict[str, object] = {
        "type": "hello",
        "protocol": 1,
        "schema": 1,
        "agent_id": "nas-primary",
        "version": "0.1.0",
        "token": TOKEN,
        "actions": sorted(SCHEMAS),
    }
    value.update(changes)
    return json.dumps(value)


def _system(*, reachable: bool = True) -> dict[str, object]:
    if not reachable:
        return {"reachable": False, "error": "truenas_unavailable"}
    return {
        "reachable": True,
        "hostname": "tank",
        "version": "25.10.0",
        "uptime_seconds": 123.0,
        "system_state": "online",
    }


def _jellyfin(*, reachable: bool = True) -> dict[str, object]:
    if not reachable:
        return {"reachable": False, "error": "backend_unavailable"}
    return {
        "reachable": True,
        "ready": True,
        "version": "10.10.7",
        "http_status": 200,
    }


def _heartbeat(
    connection_id: str,
    *,
    sequence: int = 0,
    issued_at: float | None = None,
    key: bytes = KEY,
    system: dict[str, object] | None = None,
    jellyfin: dict[str, object] | None = None,
) -> str:
    frame = envelope(
        "heartbeat",
        connection_id,
        seq=sequence,
        state={"system": system or _system(), "jellyfin": jellyfin or _jellyfin()},
        version="0.1.0",
    )
    if issued_at is not None:
        frame["issued_at"] = issued_at
    return canonical(sign(frame, key)).decode("ascii")


def _response(
    kind: str,
    connection_id: str,
    request_id: str,
    **fields: object,
) -> str:
    return canonical(
        sign(envelope(kind, connection_id, request_id=request_id, **fields), KEY)
    ).decode("ascii")


async def _authenticate(
    hub: NasAgentHub,
    socket: Socket,
    hello: str | None = None,
) -> tuple[asyncio.Task[None], str]:
    await socket.incoming.put(hello or _hello())
    task = asyncio.create_task(hub.socket(socket))
    await asyncio.wait_for(socket.sent_event.wait(), 1)
    socket.sent_event.clear()
    return task, str(decode(socket.sent[0])["connection_id"])


async def _connected(
    hub: NasAgentHub,
    socket: Socket | None = None,
) -> tuple[Socket, asyncio.Task[None], str]:
    socket = socket or Socket()
    task, connection_id = await _authenticate(hub, socket)
    await socket.incoming.put(_heartbeat(connection_id))
    for _ in range(100):
        if hub.status()["state"] == "connected":
            return socket, task, connection_id
        await asyncio.sleep(0)
    raise AssertionError("NAS Agent did not become connected")


async def _thread_call(function, *args, **kwargs):
    result: list[object] = []

    def run() -> None:
        result.append(function(*args, **kwargs))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.001)
    thread.join()
    return result[0]


def test_exact_identity_token_and_action_set_are_required(tmp_path: Path) -> None:
    async def scenario() -> None:
        for change in (
            {"agent_id": "desktop"},
            {"token": "x" * 64},
            {"actions": ["nas.agent.status"]},
            {"actions": sorted(SCHEMAS) + ["nas.middleware.call"]},
        ):
            hub = _hub(tmp_path)
            socket = Socket(_hello(**change))
            await hub.socket(socket)
            assert socket.accepted is True
            assert socket.sent == []
            assert hub.status()["state"] == "disconnected"

    asyncio.run(scenario())


def test_browser_origin_is_rejected_before_accept(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    socket = Socket(_hello())
    socket.headers["origin"] = "https://browser.invalid"
    asyncio.run(hub.socket(socket))
    assert socket.accepted is False
    assert socket.closed == [1008]


@pytest.mark.parametrize("failure", ["signature", "stale", "connection", "replay"])
def test_signed_heartbeat_rejects_tamper_stale_connection_and_replay(
    tmp_path: Path, failure: str
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected(hub)
        if failure == "signature":
            frame = decode(_heartbeat(connection_id, sequence=1))
            frame["state"]["system"]["hostname"] = "tampered"  # type: ignore[index]
            candidate = canonical(frame).decode("ascii")
        elif failure == "stale":
            candidate = _heartbeat(
                connection_id, sequence=1, issued_at=time.time() - 91
            )
        elif failure == "connection":
            candidate = _heartbeat("wrong-connection", sequence=1)
        else:
            candidate = _heartbeat(connection_id, sequence=0)
        await socket.incoming.put(candidate)
        await asyncio.wait_for(task, 1)
        assert hub.status()["state"] == "disconnected"

    asyncio.run(scenario())


def test_state_thresholds_and_disconnect_clear_local_observations(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        disabled = NasAgentHub(NasAgentIngressSettings(), monotonic=clock.monotonic)
        assert disabled.status()["state"] == "not_configured"
        hub = _hub(tmp_path, clock=clock)
        assert hub.status()["state"] == "disconnected"
        socket = Socket()
        task, connection_id = await _authenticate(hub, socket)
        assert hub.status()["state"] == "awaiting_heartbeat"
        await socket.incoming.put(_heartbeat(connection_id))
        await asyncio.sleep(0)
        assert hub.status()["state"] == "connected"
        assert hub.status()["system"] == _system()
        clock.advance(30)
        assert hub.status()["state"] == "heartbeat_aging"
        clock.advance(15)
        assert hub.status()["state"] == "heartbeat_stale"
        await socket.incoming.put(None)
        await task
        status = hub.status()
        assert status["state"] == "disconnected"
        assert status["system"] is None
        assert status["jellyfin"] is None
        assert status["observed_at"] is None

    asyncio.run(scenario())


def test_agent_can_be_healthy_while_truenas_or_jellyfin_is_down(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket = Socket()
        task, connection_id = await _authenticate(hub, socket)
        await socket.incoming.put(
            _heartbeat(
                connection_id,
                system=_system(reachable=False),
                jellyfin=_jellyfin(reachable=False),
            )
        )
        await asyncio.sleep(0)
        status = hub.status()
        assert status["state"] == "connected"
        assert status["system"] == _system(reachable=False)
        assert status["jellyfin"] == _jellyfin(reachable=False)
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_new_connection_supersedes_old_without_old_cleanup_clobbering_it(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        old, old_task, old_id = await _connected(hub)
        new, new_task, new_id = await _connected(hub)
        assert new_id != old_id
        assert 1012 in old.closed
        await old.incoming.put(None)
        await old_task
        assert hub.connection_id == new_id
        assert hub.status()["state"] == "connected"
        await new.incoming.put(None)
        await new_task

    asyncio.run(scenario())


def test_typed_status_round_trip_and_unknown_fields_fail_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected(hub)
        call = asyncio.create_task(_thread_call(hub.system_status))
        while len(socket.sent) < 2:
            await asyncio.sleep(0.001)
        request = verify(decode(socket.sent[1]), KEY, connection_id)
        await socket.incoming.put(
            _response("ack", connection_id, request["request_id"])
        )
        result = {
            "action": "nas.system.status",
            "target": "nas-primary",
            "started_at": time.time(),
            "success": True,
            **_system(),
            "unexpected": "middleware-blob",
            "completed_at": time.time(),
            "duration_seconds": 0.01,
        }
        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                request["request_id"],
                result=result,
                duplicate=False,
            )
        )
        value = await call
        assert value["success"] is False
        assert value["error"] in {"agent_disconnected", "malformed_result"}
        await task

    asyncio.run(scenario())


def test_duplicate_request_id_is_rejected_while_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=2)
        socket, task, connection_id = await _connected(hub)
        request_id = str(uuid.uuid4())
        first = asyncio.create_task(
            hub._request_async(
                socket,
                connection_id,
                "nas.system.status",
                {},
                request_id,
                str(uuid.uuid4()),
            )
        )
        while request_id not in hub._pending:
            await asyncio.sleep(0)
        duplicate = await hub._request_async(
            socket,
            connection_id,
            "nas.system.status",
            {},
            request_id,
            str(uuid.uuid4()),
        )
        assert duplicate["error"] == "duplicate_request"
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_shutdown_disconnect_after_ack_is_indeterminate_not_false_refusal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected(hub)
        call = asyncio.create_task(
            _thread_call(hub.shutdown, idempotency_key=str(uuid.uuid4()))
        )
        while len(socket.sent) < 2:
            await asyncio.sleep(0.001)
        request = verify(decode(socket.sent[1]), KEY, connection_id)
        assert request["action"] == "nas.system.shutdown"
        assert request["parameters"] == {}
        await socket.incoming.put(
            _response("ack", connection_id, request["request_id"])
        )
        await socket.incoming.put(None)
        await task
        result = await call
        assert result["action"] == "nas.system.shutdown"
        assert result["success"] is False
        assert result["error"] == "shutdown_result_indeterminate"
        assert result["indeterminate"] is True
        assert result["transport"] == "nas_agent_wss"

    asyncio.run(scenario())


def test_shutdown_disconnect_before_ack_is_transport_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, _ = await _connected(hub)
        call = asyncio.create_task(
            _thread_call(hub.shutdown, idempotency_key=str(uuid.uuid4()))
        )
        while len(socket.sent) < 2:
            await asyncio.sleep(0.001)
        await socket.incoming.put(None)
        await task
        result = await call
        assert result["success"] is False
        assert result["error"] == "agent_disconnected"
        assert "indeterminate" not in result

    asyncio.run(scenario())


def test_well_formed_late_ack_and_result_are_consumed_without_disconnect(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected(hub)
        request_id = str(uuid.uuid4())
        hub._remember_timeout(connection_id, request_id, "nas.system.status")
        ack = decode(_response("ack", connection_id, request_id))
        hub._handle_response(ack, socket, connection_id)
        result = {
            "action": "nas.system.status",
            "target": "nas-primary",
            "started_at": time.time(),
            "success": True,
            **_system(),
            "completed_at": time.time(),
            "duration_seconds": 0.01,
        }
        terminal = decode(
            _response(
                "result",
                connection_id,
                request_id,
                result=result,
                duplicate=False,
            )
        )
        hub._handle_response(terminal, socket, connection_id)
        assert hub.status()["state"] == "connected"
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_accepted_shutdown_result_is_fixed_and_records_transition(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected(hub)
        call = asyncio.create_task(
            _thread_call(hub.shutdown, idempotency_key=str(uuid.uuid4()))
        )
        while len(socket.sent) < 2:
            await asyncio.sleep(0.001)
        request = verify(decode(socket.sent[1]), KEY, connection_id)
        result = {
            "action": "nas.system.shutdown",
            "target": "nas-primary",
            "started_at": time.time(),
            "success": True,
            "accepted": True,
            "state": "scheduled",
            "method": "system.shutdown",
            "completed_at": time.time(),
            "duration_seconds": 0.01,
        }
        await socket.incoming.put(
            _response("ack", connection_id, request["request_id"])
        )
        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                request["request_id"],
                result=result,
                duplicate=False,
            )
        )
        observed = await call
        assert observed["accepted"] is True
        assert observed["method"] == "system.shutdown"
        assert hub.status()["system"]["system_state"] == "shutting_down"
        assert hub.status()["shutdown_accepted_at"] is not None
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_arbitrary_shutdown_input_and_invalid_request_id_never_send(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, _ = await _connected(hub)
        injected = await _thread_call(
            hub._request,
            "nas.system.shutdown",
            {"mode": "reboot", "delay": 0},
        )
        invalid_id = await _thread_call(
            hub.shutdown,
            idempotency_key="not-a-uuid",
        )
        assert injected["error"] == "invalid_parameter"
        assert invalid_id["error"] == "invalid_request_id"
        assert len(socket.sent) == 1
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())
