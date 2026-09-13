"""Slice 3 Desktop Agent application effector and policy boundary tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from butters.actions.agent import AgentHub
from butters.actions.coordinator import ActionCoordinator
from butters.actions.store import ActionStateStore
from butters.assistant_config import (
    ActionSettings,
    AgentIngressSettings,
    load_assistant_settings,
)
from butters.planner.model import PlannerError
from butters.planner.validator import PlannerValidator
from butters.skills.desktop_agent import (
    _job_idempotency_key,
    _launch_timeout_seconds,
    register_desktop_agent_skills,
)
from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    SkillAudience,
)
from butters.skills.policy import PolicyValidator
from butters.skills.registry import SkillRegistry
from butters.web.service import CONVERSATIONAL_PLANNER_ACTIONS
from butters_agent import AGENT_VERSION
from butters_agent.engine import Engine
from butters_agent.platform.fake import Platform
from butters_agent.protocol import (
    SCHEMAS,
    ProtocolError,
    ReplayCache,
    canonical,
    decode,
    envelope,
    sign,
    verify,
)
from butters_agent.protocol import (
    request as validate_request,
)

TOKEN = "a" * 64
KEY = b"k" * 32


class ServerSocket:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
        self.outgoing: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[str] = []
        self.sent_event = asyncio.Event()
        self.closed: list[int] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        value = await self.incoming.get()
        if value is None:
            raise ConnectionError("closed")
        return value

    async def send_text(self, value: str) -> None:
        self.sent.append(value)
        self.sent_event.set()
        await self.outgoing.put(value)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def _credentials(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "command.key"
    key.write_text(KEY.hex(), encoding="utf-8")
    key.chmod(0o640)
    config = tmp_path / "desktop-agent.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                "protocol_version = 1",
                'agent_id = "desktop"',
                f'token_sha256 = "{hashlib.sha256(TOKEN.encode()).hexdigest()}"',
                f'command_key_file = "{key}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    config.chmod(0o640)
    return config


def _hub(tmp_path: Path, *, timeout: int = 5) -> AgentHub:
    return AgentHub(
        AgentIngressSettings(
            enabled=True,
            config_path=_credentials(tmp_path),
            request_timeout_seconds=timeout,
        ).validated()
    )


def _hello() -> str:
    return json.dumps(
        {
            "type": "hello",
            "protocol": 1,
            "schema": 1,
            "agent_id": "desktop",
            "version": AGENT_VERSION,
            "token": TOKEN,
            "actions": sorted(SCHEMAS),
        }
    )


def _heartbeat(
    connection_id: str, *, interactive: bool = True, sequence: int = 0
) -> str:
    return canonical(
        sign(
            envelope(
                "heartbeat",
                connection_id,
                seq=sequence,
                session={
                    "state": "ACTIVE" if interactive else "NONE",
                    "gui_launch": interactive,
                    "interactive_session": interactive,
                },
                version=AGENT_VERSION,
            ),
            KEY,
        )
    ).decode()


async def _connected_server(hub: AgentHub, *, interactive: bool = True):
    socket = ServerSocket()
    await socket.incoming.put(_hello())
    task = asyncio.create_task(hub.socket(socket))
    while not socket.sent:
        await socket.sent_event.wait()
    socket.sent_event.clear()
    connection_id = str(decode(socket.sent[0])["connection_id"])
    await socket.incoming.put(_heartbeat(connection_id, interactive=interactive))
    for _ in range(100):
        if hub.status()["state"] == "connected":
            break
        await asyncio.sleep(0)
    assert hub.status()["state"] == "connected"
    return socket, task, connection_id


async def _connected_pair(hub: AgentHub, engine: Engine):
    socket, server, connection_id = await _connected_server(hub)
    cache = ReplayCache()

    async def respond() -> None:
        index = 1  # welcome is frame zero
        while True:
            while len(socket.sent) <= index:
                await asyncio.sleep(0.001)
            frame = verify(decode(socket.sent[index]), KEY, connection_id)
            index += 1
            if frame["type"] == "cancel":
                continue
            try:
                validate_request(frame, target="desktop")
                cached = cache.get(frame)
                await socket.incoming.put(
                    _response("ack", connection_id, frame["request_id"])
                )
                duplicate = cached is not None
                result = cached or engine.invoke(frame["action"], frame["parameters"])
                if cached is None:
                    cache.put(frame, result)
                await socket.incoming.put(
                    _response(
                        "result",
                        connection_id,
                        frame["request_id"],
                        result=result,
                        duplicate=duplicate,
                    )
                )
            except ProtocolError as exc:
                await socket.incoming.put(
                    _response(
                        "error",
                        connection_id,
                        frame["request_id"],
                        error=str(exc),
                    )
                )

    agent = asyncio.create_task(respond())
    return socket, server, agent


async def _stop_pair(socket: ServerSocket, server, agent) -> None:
    agent.cancel()
    await socket.incoming.put(None)
    await server
    await asyncio.gather(agent, return_exceptions=True)


async def _call(function, *args, **kwargs):
    result: list[object] = []

    def run() -> None:
        result.append(function(*args, **kwargs))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while thread.is_alive():
        await asyncio.sleep(0.001)
    thread.join()
    return result[0]


@pytest.fixture
def engine() -> Engine:
    value = Engine(Platform())
    value.apps = {
        "git_bash": {
            "path": r"C:\Program Files\Git\git-bash.exe",
            "images": [r"C:\Program Files\Git\usr\bin\mintty.exe"],
        },
        "parsec": {
            "path": r"C:\Program Files\Parsec\parsecd.exe",
            "images": [r"C:\Program Files\Parsec\parsecd.exe"],
        },
    }
    return value


def test_job_identity_derives_deterministic_protocol_valid_uuid4() -> None:
    first = _job_idempotency_key("coordinator-job-one")
    same = _job_idempotency_key("coordinator-job-one")
    different = _job_idempotency_key("coordinator-job-two")

    parsed = uuid.UUID(first)
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    frame = sign(
        envelope(
            "request",
            "c" * 64,
            request_id=str(uuid.uuid4()),
            action="desktop.app.launch",
            target="desktop",
            parameters={"app": "parsec"},
            idempotency_key=first,
            timeout_seconds=30,
        ),
        KEY,
    )
    validate_request(frame, target="desktop")
    assert frame["idempotency_key"] == first
    assert first == same
    assert first != different


def test_launch_without_stable_job_identity_fails_closed() -> None:
    class Hub:
        settings = SimpleNamespace(enabled=True, request_timeout_seconds=1)
        configured = True
        called = False

        def launch_app(self, *_args, **_kwargs):
            self.called = True
            return {"success": True}

    hub = Hub()
    registry = SkillRegistry(
        PolicyValidator(
            allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
        )
    )
    register_desktop_agent_skills(registry, hub)  # type: ignore[arg-type]
    execution = registry.execute(
        "desktop.app.launch",
        {"app": "parsec"},
        administrator=True,
        action_authorization=ActionAuthorization(
            frozenset({"desktop.app.launch"}), "direct_user_request", True
        ),
        authentication_context=AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            "session",
            "identity",
            time.time() + 30,
            "unit_test",
        ),
        session_id="session",
        identity="identity",
        job_id=None,
    )
    assert execution.failure is not None
    assert execution.failure.code == "internal_error"
    assert "stable coordinator job identity" in execution.failure.message
    assert hub.called is False


def test_launch_timeout_covers_two_request_windows() -> None:
    request_timeout = 30
    configured = _launch_timeout_seconds(request_timeout)
    hub = SimpleNamespace(
        settings=SimpleNamespace(enabled=True, request_timeout_seconds=request_timeout),
        configured=True,
    )
    registry = SkillRegistry()
    register_desktop_agent_skills(registry, hub)
    launch = registry.get("desktop.app.launch")
    assert launch is not None
    assert launch.timeout_seconds == configured
    assert configured > 2 * request_timeout
    assert configured == 62


def test_app_list_status_launch_and_safe_projection(
    tmp_path: Path, engine: Engine
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, server, agent = await _connected_pair(hub, engine)
        listed = await _call(hub.list_apps)
        assert listed["success"] is True
        assert [item["app"] for item in listed["apps"]] == ["git_bash", "parsec"]
        encoded = json.dumps(listed).lower()
        assert "program files" not in encoded
        assert "\\git\\" not in encoded
        status = await _call(hub.app_status, "parsec")
        assert status["status"] == "not_running"
        first = await _call(hub.launch_app, "parsec")
        second = await _call(hub.launch_app, "parsec")
        assert first["outcome"] == "launched"
        assert second["outcome"] == "already_running"
        assert engine.platform.launches == 1
        await _stop_pair(socket, server, agent)

    asyncio.run(scenario())


def test_app_projection_drops_agent_supplied_executable_metadata() -> None:
    projected = AgentHub._project_app(
        {
            "app": "parsec",
            "installed": True,
            "running": False,
            "path": r"C:\Program Files\Parsec\parsecd.exe",
            "command": "powershell attacker-controlled",
            "pids": [1234],
        }
    )
    assert projected == {
        "app": "parsec",
        "status": "not_running",
        "available": True,
    }


def test_agent_failure_projection_drops_untrusted_fields() -> None:
    failure = AgentHub._public_failure(
        "desktop.app.launch",
        {
            "success": False,
            "error": "launch_failed",
            "path": r"C:\secret.exe",
            "stderr": "sensitive",
        },
    )
    assert failure == {
        "action": "desktop.app.launch",
        "success": False,
        "error": "launch_failed",
    }


def test_unknown_app_is_rejected_before_status_or_launch(
    tmp_path: Path, engine: Engine
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, server, agent = await _connected_pair(hub, engine)
        assert (await _call(hub.app_status, "unknown"))["error"] == "unknown_app"
        assert (await _call(hub.launch_app, "unknown"))["error"] == "unknown_app"
        assert engine.platform.launches == 0
        await _stop_pair(socket, server, agent)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field",
    [
        "path",
        "executable",
        "command",
        "argv",
        "shell",
        "powershell",
        "working_dir",
        "env",
        "host",
        "ip",
        "mac",
        "user",
        "password",
    ],
)
def test_skill_schema_rejects_every_machine_level_parameter(field: str) -> None:
    hub = SimpleNamespace(
        settings=SimpleNamespace(enabled=True, request_timeout_seconds=30),
        configured=True,
    )
    registry = SkillRegistry(
        PolicyValidator(
            allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
        )
    )
    register_desktop_agent_skills(registry, hub)
    execution = registry.execute(
        "desktop.app.launch",
        {"app": "parsec", field: "attacker-controlled"},
        administrator=True,
    )
    assert execution.failure is not None
    assert execution.failure.code == "invalid_arguments"


@pytest.mark.parametrize(
    "app", [r"C:\evil.exe", "parsec --flag", "parsec;shutdown", "192.168.1.2"]
)
def test_app_value_injection_is_rejected(app: str) -> None:
    hub = SimpleNamespace(
        settings=SimpleNamespace(enabled=True, request_timeout_seconds=30),
        configured=True,
    )
    registry = SkillRegistry()
    register_desktop_agent_skills(registry, hub)
    execution = registry.execute("desktop.app.status", {"app": app}, administrator=True)
    assert execution.failure is not None
    assert execution.failure.code == "invalid_arguments"


@pytest.mark.parametrize(
    ("action", "arguments"),
    [
        ("desktop.app.list", {"app": "parsec"}),
        ("desktop.app.status", {"app": "parsec", "path": r"C:\evil.exe"}),
    ],
)
def test_read_only_app_schemas_reject_extra_parameters(
    action: str, arguments: dict[str, object]
) -> None:
    hub = SimpleNamespace(
        settings=SimpleNamespace(enabled=True, request_timeout_seconds=30),
        configured=True,
    )
    registry = SkillRegistry()
    register_desktop_agent_skills(registry, hub)
    execution = registry.execute(action, arguments, administrator=True)
    assert execution.failure is not None
    assert execution.failure.code == "invalid_arguments"


def test_disconnected_stale_and_missing_session_are_unavailable(tmp_path: Path) -> None:
    assert _hub(tmp_path).list_apps()["error"] == "agent_unavailable"

    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, _ = await _connected_server(hub, interactive=False)
        assert (await _call(hub.launch_app, "parsec"))["error"] == (
            "interactive_session_unavailable"
        )
        hub.last_authenticated_activity -= hub.settings.heartbeat_stale_seconds
        assert (await _call(hub.list_apps))["error"] == "agent_unavailable"
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def _response(kind: str, connection_id: str, request_id: str, **fields: object) -> str:
    return canonical(
        sign(envelope(kind, connection_id, request_id=request_id, **fields), KEY)
    ).decode()


async def _next_request(socket: ServerSocket) -> dict[str, object]:
    while True:
        for raw in socket.sent[1:]:
            frame = decode(raw)
            if frame.get("type") == "request":
                return frame
        await asyncio.sleep(0.001)


def test_request_timeout_and_disconnect_lifecycle(tmp_path: Path) -> None:
    async def timeout_scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        await socket.incoming.put(
            _response("ack", connection_id, request["request_id"])
        )
        assert (await call)["error"] == "timeout"
        await socket.incoming.put(None)
        await task

    async def disconnect_scenario() -> None:
        hub = _hub(tmp_path / "disconnect")
        socket, task, _ = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        await _next_request(socket)
        await socket.incoming.put(None)
        await task
        assert (await call)["error"] == "agent_disconnected"

    asyncio.run(timeout_scenario())
    asyncio.run(disconnect_scenario())


def test_valid_late_terminal_after_timeout_is_ignored_and_timeout_stands(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        socket, task, connection_id = await _connected_server(hub)
        first_call = asyncio.create_task(_call(hub.list_apps))
        first_request = await _next_request(socket)
        await socket.incoming.put(
            _response("ack", connection_id, first_request["request_id"])
        )
        first_result = await first_call
        assert first_result["error"] == "timeout"

        # A well-formed late result is consumed only as a tombstoned terminal
        # frame. It cannot alter the already-returned timeout.
        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                first_request["request_id"],
                result={"action": "desktop.app.list", "success": True, "apps": []},
                duplicate=False,
            )
        )
        second_call = asyncio.create_task(_call(hub.list_apps))
        for _ in range(1000):
            requests = [
                decode(raw)
                for raw in socket.sent[1:]
                if decode(raw).get("type") == "request"
            ]
            if len(requests) == 2:
                break
            await asyncio.sleep(0.001)
        assert len(requests) == 2
        second_request = requests[-1]
        await socket.incoming.put(
            _response("ack", connection_id, second_request["request_id"])
        )
        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                second_request["request_id"],
                result={"action": "desktop.app.list", "success": True, "apps": []},
                duplicate=False,
            )
        )
        assert (await second_call)["success"] is True
        assert hub.status()["state"] == "connected"
        assert first_result["error"] == "timeout"
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_valid_late_ack_preserves_timeout_and_tombstone_for_late_terminal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        hub._ACK_TIMEOUT_SECONDS = 0.01
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        request_id = str(request["request_id"])
        result = await call
        tombstone = (connection_id, request_id)
        assert result["error"] == "timeout"
        assert tombstone in hub._timed_out

        initial_activity = hub.last_authenticated_activity
        await socket.incoming.put(_response("ack", connection_id, request_id))
        await socket.incoming.put(_heartbeat(connection_id, sequence=1))
        for _ in range(100):
            if hub.last_authenticated_activity != initial_activity:
                break
            await asyncio.sleep(0)
        assert hub.last_authenticated_activity != initial_activity
        assert not task.done()
        assert hub.status()["state"] == "connected"
        assert result["error"] == "timeout"
        assert tombstone in hub._timed_out

        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                request_id,
                result={"action": "desktop.app.list", "success": True, "apps": []},
                duplicate=False,
            )
        )
        for _ in range(100):
            if tombstone not in hub._timed_out:
                break
            await asyncio.sleep(0)
        assert tombstone not in hub._timed_out
        assert not task.done()
        assert hub.status()["state"] == "connected"
        assert result["error"] == "timeout"
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_unknown_late_ack_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected_server(hub)
        await socket.incoming.put(
            _response("ack", connection_id, str(uuid.uuid4()))
        )
        await task
        assert hub.reason == "wrong_request_id"
        assert hub.status()["state"] == "disconnected"

    asyncio.run(scenario())


def test_malformed_late_ack_is_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        hub._ACK_TIMEOUT_SECONDS = 0.01
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        request_id = str(request["request_id"])
        assert (await call)["error"] == "timeout"
        malformed = envelope(
            "ack", connection_id, request_id=request_id, unexpected=True
        )
        await socket.incoming.put(canonical(sign(malformed, KEY)).decode())
        await task
        assert hub.reason == "replayed_message"
        assert hub.status()["state"] == "disconnected"

    asyncio.run(scenario())


def test_superseded_connection_late_ack_cannot_affect_current_connection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        hub._ACK_TIMEOUT_SECONDS = 0.01
        old, old_task, old_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(old)
        request_id = str(request["request_id"])
        assert (await call)["error"] == "timeout"
        assert (old_id, request_id) in hub._timed_out

        new, new_task, new_id = await _connected_server(hub)
        assert new_id != old_id
        assert (old_id, request_id) not in hub._timed_out
        await old.incoming.put(_response("ack", old_id, request_id))
        await old_task
        assert hub.connection_id == new_id
        assert hub.status()["state"] == "connected"
        await new.incoming.put(None)
        await new_task

    asyncio.run(scenario())


def test_live_ack_replay_still_disconnects_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        ack = _response("ack", connection_id, str(request["request_id"]))
        await socket.incoming.put(ack)
        await socket.incoming.put(ack)
        await task
        assert (await call)["success"] is False
        assert hub.reason == "replayed_message"
        assert hub.status()["state"] == "disconnected"

    asyncio.run(scenario())


def test_timed_out_request_tombstones_are_bounded_and_expire(
    tmp_path: Path,
) -> None:
    now = [100.0]
    hub = AgentHub(
        AgentIngressSettings(
            enabled=True,
            config_path=_credentials(tmp_path),
            request_timeout_seconds=1,
        ).validated(),
        monotonic=lambda: now[0],
    )
    for index in range(hub._LATE_RESULT_CAPACITY + 1):
        hub._remember_timeout("connection", str(uuid.uuid4()), f"action-{index}")
    assert len(hub._timed_out) == hub._LATE_RESULT_CAPACITY
    now[0] += hub._LATE_RESULT_TTL_SECONDS + 0.001
    hub._prune_timeouts()
    assert not hub._timed_out


def test_connection_supersession_fails_old_work_and_rejects_old_result(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        old, old_task, old_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(old)
        new = ServerSocket()
        await new.incoming.put(_hello())
        new_task = asyncio.create_task(hub.socket(new))
        while not new.sent:
            await asyncio.sleep(0)
        new_id = str(decode(new.sent[0])["connection_id"])
        assert new_id != old_id
        assert (await call)["error"] == "superseded_connection"
        old_result = {
            "action": "desktop.app.list",
            "success": True,
            "apps": [],
        }
        await old.incoming.put(
            _response(
                "result",
                old_id,
                request["request_id"],
                result=old_result,
                duplicate=False,
            )
        )
        await old_task
        assert hub.connection_id == new_id
        await new.incoming.put(None)
        await new_task

    asyncio.run(scenario())


def test_catalog_from_superseded_connection_cannot_authorize_request(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        old, old_task, old_id = await _connected_server(hub)
        old_call = asyncio.create_task(_call(hub.list_apps))
        old_request = await _next_request(old)
        await old.incoming.put(_response("ack", old_id, old_request["request_id"]))
        await old.incoming.put(
            _response(
                "result",
                old_id,
                old_request["request_id"],
                result={
                    "action": "desktop.app.list",
                    "success": True,
                    "apps": [{"app": "parsec", "installed": True, "running": False}],
                },
                duplicate=False,
            )
        )
        assert (await old_call)["apps"][0]["app"] == "parsec"

        new, new_task, new_id = await _connected_server(hub)
        assert new_id != old_id
        assert hub._app_catalog == {}
        assert hub._catalog_connection_id is None
        await old.incoming.put(None)
        await old_task

        status_call = asyncio.create_task(_call(hub.app_status, "parsec"))
        new_request = await _next_request(new)
        assert new_request["action"] == "desktop.app.list"
        await new.incoming.put(_response("ack", new_id, new_request["request_id"]))
        await new.incoming.put(
            _response(
                "result",
                new_id,
                new_request["request_id"],
                result={
                    "action": "desktop.app.list",
                    "success": True,
                    "apps": [{"app": "git_bash", "installed": True, "running": False}],
                },
                duplicate=False,
            )
        )
        assert (await status_call)["error"] == "unknown_app"
        assert [
            decode(raw)["action"]
            for raw in new.sent[1:]
            if decode(raw).get("type") == "request"
        ] == ["desktop.app.list"]
        await new.incoming.put(None)
        await new_task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "case", ["invalid_signature", "wrong_id", "duplicate_result", "malformed_result"]
)
def test_invalid_or_replayed_results_are_rejected(tmp_path: Path, case: str) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        request_id = str(request["request_id"])
        ack = _response("ack", connection_id, request_id)
        if case == "invalid_signature":
            frame = decode(ack)
            frame["request_id"] = str(uuid.uuid4())
            await socket.incoming.put(canonical(frame).decode())
        elif case == "wrong_id":
            await socket.incoming.put(
                _response("ack", connection_id, str(uuid.uuid4()))
            )
        elif case == "duplicate_result":
            await socket.incoming.put(ack)
            result = _response(
                "result",
                connection_id,
                request_id,
                result={"action": "desktop.app.list", "success": True, "apps": []},
                duplicate=False,
            )
            await socket.incoming.put(result)
            await socket.incoming.put(result)
        else:
            await socket.incoming.put(
                _response(
                    "result",
                    connection_id,
                    request_id,
                    result={"action": "desktop.app.list", "success": True, "apps": []},
                    duplicate=False,
                )
            )
        await task
        outcome = await call
        assert outcome["success"] is False
        expected = {
            "invalid_signature": "invalid_signature",
            "wrong_id": "wrong_request_id",
            "duplicate_result": "replayed_message",
            "malformed_result": "malformed_result",
        }[case]
        assert hub.reason == expected

    asyncio.run(scenario())


def test_unidentifiable_error_does_not_claim_only_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, task, connection_id = await _connected_server(hub)
        call = asyncio.create_task(_call(hub.list_apps))
        request = await _next_request(socket)
        unidentified = canonical(
            sign(envelope("error", connection_id, error="malformed_request"), KEY)
        ).decode()
        await socket.incoming.put(unidentified)
        await socket.incoming.put(
            _response("ack", connection_id, request["request_id"])
        )
        await socket.incoming.put(
            _response(
                "result",
                connection_id,
                request["request_id"],
                result={"action": "desktop.app.list", "success": True, "apps": []},
                duplicate=False,
            )
        )
        assert (await call)["success"] is True
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_idempotency_retry_and_conflict_fail_closed(
    tmp_path: Path, engine: Engine
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        socket, server, agent = await _connected_pair(hub, engine)
        key = str(uuid.uuid4())
        first = await _call(hub.launch_app, "git_bash", idempotency_key=key)
        replay = await _call(hub.launch_app, "git_bash", idempotency_key=key)
        conflict = await _call(hub.launch_app, "parsec", idempotency_key=key)
        assert first["success"] is True and replay["success"] is True
        assert conflict["error"] == "duplicate_request"
        assert engine.platform.launches == 1
        await _stop_pair(socket, server, agent)

    asyncio.run(scenario())


def test_real_coordinator_policy_to_hub_transport_seam(
    tmp_path: Path, engine: Engine
) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path, timeout=1)
        socket, server, agent = await _connected_pair(hub, engine)
        registry = SkillRegistry(
            PolicyValidator(
                allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
            )
        )
        register_desktop_agent_skills(registry, hub)

        # Registry policy still refuses a launch that did not come through an
        # authorized frozen ActionCoordinator plan, before the hub is called.
        denied = registry.execute(
            "desktop.app.launch",
            {"app": "parsec"},
            administrator=True,
            job_id="not-an-authorized-coordinator-job",
        )
        assert denied.failure is not None
        assert denied.failure.code == "action_confirmation_required"
        assert [decode(raw)["type"] for raw in socket.sent] == ["welcome"]

        store = ActionStateStore(tmp_path / "actions.sqlite3", ActionSettings())
        coordinator = ActionCoordinator(registry, store)
        plan = coordinator.freeze(
            skill="desktop.app.launch",
            arguments={"app": "parsec"},
            summary="Launch allowlisted app through the real hub",
            session_id="session",
            identity="identity",
            request_id=str(uuid.uuid4()),
            source="direct_user_request",
        )
        jobs = coordinator.execute(
            plan.plan_id,
            session_id="session",
            identity="identity",
            authentication=AuthenticationContext(
                AuthenticationLevel.ELEVATED,
                "session",
                "identity",
                time.time() + 30,
                "unit_test",
            ),
        )
        job_id = str(jobs[0]["job_id"])
        for _ in range(1000):
            job = store.job(job_id, session_id="session", identity="identity")
            if job["state"] in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.001)
        assert job["state"] == "completed", job

        connection_id = str(decode(socket.sent[0])["connection_id"])
        requests = [
            verify(decode(raw), KEY, connection_id)
            for raw in socket.sent[1:]
            if decode(raw).get("type") == "request"
        ]
        assert [frame["action"] for frame in requests] == [
            "desktop.app.list",
            "desktop.app.launch",
        ]
        for frame in requests:
            validate_request(frame, target="desktop")
        launch = requests[-1]
        expected = _job_idempotency_key(job_id)
        assert launch["idempotency_key"] == expected
        assert _job_idempotency_key(job_id) == expected
        assert uuid.UUID(str(launch["idempotency_key"])).version == 4
        assert engine.platform.launches == 1
        await _stop_pair(socket, server, agent)

    asyncio.run(scenario())


def test_registry_metadata_policy_coordinator_and_catalog_boundaries(
    tmp_path: Path,
) -> None:
    launched = threading.Event()

    class Hub:
        settings = SimpleNamespace(enabled=True, request_timeout_seconds=1)
        configured = True

        def list_apps(self):
            return {
                "success": True,
                "apps": [{"app": "parsec", "status": "not_running"}],
            }

        def app_status(self, app):
            return {"success": True, "app": app, "status": "not_running"}

        def launch_app(self, app, **_kwargs):
            launched.set()
            return {"success": True, "app": app, "outcome": "launched"}

    registry = SkillRegistry(
        PolicyValidator(
            allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
        )
    )
    register_desktop_agent_skills(registry, Hub())
    listed = registry.get("desktop.app.list")
    launch = registry.get("desktop.app.launch")
    assert listed.action_class is ActionClass.READ_ONLY
    assert listed.audience is SkillAudience.ADMINISTRATOR
    assert listed.input_schema["additionalProperties"] is False
    assert launch.action_class is ActionClass.ACTION
    assert launch.authentication is AuthenticationLevel.ELEVATED
    assert launch.confirmation_required is False
    assert launch.explicit_intent_required is True

    denied = registry.execute(
        "desktop.app.launch", {"app": "parsec"}, administrator=True
    )
    assert denied.failure.code == "action_confirmation_required"
    assert not launched.is_set()

    store = ActionStateStore(tmp_path / "actions.sqlite3", ActionSettings())
    coordinator = ActionCoordinator(registry, store)
    plan = coordinator.freeze(
        skill="desktop.app.launch",
        arguments={"app": "parsec"},
        summary="Launch allowlisted app",
        session_id="session",
        identity="identity",
        request_id=str(uuid.uuid4()),
        source="direct_user_request",
    )
    jobs = coordinator.execute(
        plan.plan_id,
        session_id="session",
        identity="identity",
        authentication=AuthenticationContext(
            AuthenticationLevel.ELEVATED,
            "session",
            "identity",
            time.time() + 30,
            "unit_test",
        ),
    )
    assert jobs and launched.wait(1)

    assert (
        not {
            "desktop.app.list",
            "desktop.app.status",
            "desktop.app.launch",
        }
        & CONVERSATIONAL_PLANNER_ACTIONS
    )
    catalog = PlannerValidator(registry).catalog(CONVERSATIONAL_PLANNER_ACTIONS)
    with pytest.raises(PlannerError, match="unknown action"):
        PlannerValidator(registry).validate(
            {
                "summary": "Launch app",
                "rationale": "Requested",
                "requires_confirmation": False,
                "steps": [
                    {"action_id": "desktop.app.launch", "parameters": {"app": "parsec"}}
                ],
            },
            catalog=catalog,
            administrator=True,
        )


def test_public_hub_surface_has_no_generic_command_api() -> None:
    public = {name for name in dir(AgentHub) if not name.startswith("_")}
    assert {"list_apps", "app_status", "launch_app"} <= public
    assert not {"invoke", "execute", "send", "send_command"} & public


def test_default_gates_and_no_desktop_admin_route() -> None:
    settings = load_assistant_settings()
    assert settings.agent_ingress.enabled is False
    ingress = Path(__file__).parents[1] / "config/agent-ingress.example.toml"
    assert "enabled = false" in ingress.read_text(encoding="utf-8")
    app_source = (Path(__file__).parents[1] / "src/butters/web/app.py").read_text()
    assert '"/api/desktop/actions"' not in app_source
