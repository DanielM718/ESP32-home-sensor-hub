"""Observer-only Desktop Agent ingress and state trust-boundary tests."""

from __future__ import annotations

import asyncio
import grp
import hashlib
import json
import os
import pwd
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from butters.actions.agent import AgentHub
from butters.actions.agent_ingress import (
    bind_addresses,
    load_config,
    validate_handshake,
)
from butters.assistant_config import (
    AgentIngressSettings,
    ConfigError,
    load_assistant_settings,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import CONVERSATIONAL_PLANNER_ACTIONS, BetaAssistantService
from butters_agent import AGENT_VERSION
from butters_agent.protocol import SCHEMAS, canonical, decode, envelope, sign

TOKEN = "a" * 64
KEY = b"k" * 32


class Socket:
    def __init__(self, *incoming: str, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
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
        item = await self.incoming.get()
        if item is None:
            raise ConnectionError("closed")
        return item

    async def send_text(self, value: str) -> None:
        self.sent.append(value)
        self.sent_event.set()

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)


def _credentials(tmp_path: Path) -> Path:
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


def _hub(tmp_path: Path, **changes: object) -> AgentHub:
    settings = AgentIngressSettings(
        enabled=True, config_path=_credentials(tmp_path), **changes
    ).validated()
    return AgentHub(settings)


def _hello(*, token: object = TOKEN, agent_id: str = "desktop") -> str:
    return json.dumps(
        {
            "type": "hello",
            "protocol": 1,
            "schema": 1,
            "agent_id": agent_id,
            "version": AGENT_VERSION,
            "token": token,
            "actions": sorted(SCHEMAS),
        }
    )


def _heartbeat(
    connection_id: str,
    *,
    seq: int = 0,
    issued_at: float | None = None,
    interactive: bool = True,
    key: bytes = KEY,
) -> str:
    frame = envelope(
        "heartbeat",
        connection_id,
        seq=seq,
        session={
            "state": "ACTIVE" if interactive else "NONE",
            "gui_launch": interactive,
            "interactive_session": interactive,
        },
        version=AGENT_VERSION,
    )
    if issued_at is not None:
        frame["issued_at"] = issued_at
    return canonical(sign(frame, key)).decode("ascii")


async def _authenticate(
    hub: AgentHub, socket: Socket
) -> tuple[asyncio.Task[None], str]:
    await socket.incoming.put(_hello())
    task = asyncio.create_task(hub.socket(socket))
    await asyncio.wait_for(socket.sent_event.wait(), 1)
    welcome = decode(socket.sent[0])
    return task, str(welcome["connection_id"])


async def _wait_connected(hub: AgentHub) -> None:
    for _ in range(100):
        if hub.status()["agent_connected"]:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("agent did not become connected")


def test_valid_machine_authentication_and_disconnect_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub, socket = _hub(tmp_path), Socket()
        task, connection_id = await _authenticate(hub, socket)
        assert socket.accepted
        assert hub.status()["state"] == "awaiting_heartbeat"
        await socket.incoming.put(_heartbeat(connection_id))
        await _wait_connected(hub)
        assert hub.status()["agent_connected"] is True
        assert hub.status()["interactive_session"] == "present"
        await socket.incoming.put(None)
        await task
        assert hub.status()["state"] == "disconnected"
        assert hub.status()["interactive_session"] == "unknown"
        assert hub.connection_id is None

    asyncio.run(scenario())


@pytest.mark.parametrize("token", ["b" * 64, None])
def test_invalid_or_missing_token_is_rejected(tmp_path: Path, token: object) -> None:
    hub = _hub(tmp_path)
    socket = Socket(_hello(token=token))
    asyncio.run(hub.socket(socket))
    assert socket.accepted
    assert socket.sent == []
    assert socket.closed
    assert hub.status()["state"] == "disconnected"


def test_browser_origin_is_rejected_before_accept(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    socket = Socket(_hello(), headers={"origin": "https://browser.invalid"})
    asyncio.run(hub.socket(socket))
    assert not socket.accepted
    assert socket.sent == []
    assert socket.closed == [1008]


def test_browser_cookie_or_admin_identity_cannot_substitute_for_agent_auth(
    tmp_path: Path,
) -> None:
    hub = _hub(tmp_path)
    socket = Socket(
        json.dumps({"type": "hello"}),
        headers={
            "cookie": "butters_session=administrator",
            "tailscale-user-login": "admin",
        },
    )
    asyncio.run(hub.socket(socket))
    assert socket.sent == []
    assert hub.status()["state"] == "disconnected"


@pytest.mark.parametrize("failure", ["signature", "stale", "connection", "replay"])
def test_signed_frame_stale_connection_and_replay_rejections(
    tmp_path: Path, failure: str
) -> None:
    async def scenario() -> None:
        hub, socket = _hub(tmp_path), Socket()
        task, connection_id = await _authenticate(hub, socket)
        first = _heartbeat(connection_id)
        if failure == "signature":
            frame = decode(first)
            frame["session"]["interactive_session"] = False
            candidate = canonical(frame).decode("ascii")
        elif failure == "stale":
            candidate = _heartbeat(connection_id, issued_at=time.time() - 91)
        elif failure == "connection":
            candidate = _heartbeat("wrong-connection")
        else:
            await socket.incoming.put(first)
            await _wait_connected(hub)
            candidate = first
        await socket.incoming.put(candidate)
        await asyncio.wait_for(task, 1)
        assert hub.status()["agent_connected"] is False
        expected = {
            "signature": "invalid_signature",
            "stale": "stale_request",
            "connection": "superseded_connection",
            "replay": "replayed_message",
        }[failure]
        assert hub.reason == expected

    asyncio.run(scenario())


def test_new_authenticated_connection_supersedes_old_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub = _hub(tmp_path)
        old, new = Socket(), Socket()
        old_task, old_id = await _authenticate(hub, old)
        await old.incoming.put(_heartbeat(old_id))
        await _wait_connected(hub)
        new_task, new_id = await _authenticate(hub, new)
        assert new_id != old_id
        assert 1012 in old.closed
        await new.incoming.put(_heartbeat(new_id))
        await _wait_connected(hub)
        await old.incoming.put(None)
        await old_task
        assert hub.connection_id == new_id
        assert hub.status()["agent_connected"] is True
        await new.incoming.put(None)
        await new_task

    asyncio.run(scenario())


def test_interactive_session_absent_is_reported_not_guessed(tmp_path: Path) -> None:
    async def scenario() -> None:
        hub, socket = _hub(tmp_path), Socket()
        task, connection_id = await _authenticate(hub, socket)
        assert hub.status()["interactive_session"] == "unknown"
        await socket.incoming.put(_heartbeat(connection_id, interactive=False))
        await _wait_connected(hub)
        assert hub.status()["interactive_session"] == "absent"
        facets = {f.name: f for f in hub.snapshot().facets}
        assert facets["desktop.interactive_session"].confidence == "observed"
        await socket.incoming.put(None)
        await task

    asyncio.run(scenario())


def test_default_disabled_and_configured_state_is_truthful(tmp_path: Path) -> None:
    default = AgentHub(AgentIngressSettings())
    assert default.status()["state"] == "not_configured"
    assert default.status()["interactive_session"] == "unknown"
    configured = _hub(tmp_path)
    assert configured.status()["state"] == "disconnected"
    assert configured.status()["interactive_session"] == "unknown"


def test_no_agent_action_skill_or_planner_entry_is_registered(tmp_path: Path) -> None:
    settings = load_assistant_settings()
    assert settings.agent_ingress.enabled is False
    assert not any(
        name.startswith("desktop.agent") for name in CONVERSATIONAL_PLANNER_ACTIONS
    )
    assert not any("app.launch" in name for name in CONVERSATIONAL_PLANNER_ACTIONS)
    source = (Path(__file__).parents[1] / "src/butters/actions/agent.py").read_text()
    assert "def invoke(" not in source
    assert "def send(" not in source


def test_route_gate_and_existing_admin_manual_surface_are_unchanged(
    tmp_path: Path,
) -> None:
    base = load_assistant_settings()

    def application(enabled: bool):
        settings = replace(
            base,
            agent_ingress=replace(
                base.agent_ingress,
                enabled=enabled,
                config_path=_credentials(tmp_path),
            ).validated(),
            diagnostics=replace(base.diagnostics, enabled=False),
            web=replace(
                base.web,
                state_dir=tmp_path / ("on" if enabled else "off"),
                development_mode=True,
                admin_identities=("admin@example.com",),
            ).validated(),
            remediation=replace(
                base.remediation,
                jobs_dir=tmp_path / ("jobs-on" if enabled else "jobs-off"),
            ),
        )
        service = BetaAssistantService(
            settings,
            DomainVocabulary((), ()),
            general_reasoner=SimpleNamespace(available=False),
        )
        app = create_app(
            settings,
            DomainVocabulary((), ()),
            service,
            stt_engine_factory=lambda: SimpleNamespace(close=lambda: None),
        )
        return app, service

    disabled_app, disabled_service = application(False)
    enabled_app, enabled_service = application(True)
    disabled_paths = {route.path for route in disabled_app.routes}
    enabled_paths = {route.path for route in enabled_app.routes}
    assert "/agent/v1/session" not in disabled_paths
    assert "/agent/v1/session" in enabled_paths
    assert {"/admin", "/api/admin/tools", "/api/admin/actions"} <= disabled_paths
    assert not any(path.startswith("/api/desktop") for path in enabled_paths)
    for service in (disabled_service, enabled_service):
        registered = {spec.name for spec in service.assistant.skills.skills}
        assert "get_desktop_status" in registered
        assert not any(name.startswith("desktop.agent") for name in registered)
        assert not any("app.launch" in name for name in registered)

    async def observe() -> None:
        transport = httpx.ASGITransport(app=disabled_app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get(
                "/api/admin/overview",
                headers={"tailscale-user-login": "admin@example.com"},
            )
        assert response.status_code == 200
        snapshot = response.json()["desktop_agent_state"]
        facets = {facet["name"]: facet["value"] for facet in snapshot["facets"]}
        assert facets == {
            "desktop.agent": "not_configured",
            "desktop.interactive_session": "unknown",
        }
        assert "connection_id" not in json.dumps(snapshot)

    asyncio.run(observe())


def test_application_config_parsing_and_validation(tmp_path: Path) -> None:
    original = Path(__file__).parents[1] / "config/assistant.toml"
    configured = tmp_path / "assistant.toml"
    configured.write_text(
        original.read_text()
        .replace(
            "[agent_ingress]\nenabled = false",
            f'[agent_ingress]\nenabled = true\nconfig_path = "{tmp_path}/machine.toml"',
        )
        .replace('config_path = "/etc/butters/desktop-agent.toml"\n', "", 1),
        encoding="utf-8",
    )
    settings = load_assistant_settings(configured)
    assert settings.agent_ingress.enabled is True
    assert settings.agent_ingress.config_path == tmp_path / "machine.toml"
    with pytest.raises(ConfigError):
        replace(settings.agent_ingress, config_path=Path("relative.toml")).validated()


def test_secret_reference_modes_and_invalid_secret_fail_closed(tmp_path: Path) -> None:
    config = _credentials(tmp_path)
    config.chmod(0o666)
    hub = AgentHub(AgentIngressSettings(enabled=True, config_path=config))
    assert hub.configured is False
    assert hub.status()["state"] == "not_configured"
    example = (
        Path(__file__).parents[1] / "config/desktop-agent.example.toml"
    ).read_text()
    assert TOKEN not in example
    assert "command_key_file" in example


def test_transport_config_is_default_disabled_and_root_managed(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "config/agent-ingress.example.toml"
    config = tmp_path / "agent-ingress.toml"
    config.write_text(example.read_text(), encoding="utf-8")
    config.chmod(0o640)
    loaded = load_config(config)
    assert loaded.enabled is False
    assert loaded.upstream_host == "127.0.0.1"
    config.chmod(0o666)
    with pytest.raises(ValueError, match="unsafe_configuration"):
        load_config(config)


def test_installer_and_unit_remain_inert_and_preserve_recovery_permissions() -> None:
    root = Path(__file__).parents[1]
    installer_path = root / "scripts/install-agent-ingress"
    installer = installer_path.read_text(encoding="utf-8")
    unit = (root / "systemd/butters-agent-ingress.service").read_text(encoding="utf-8")
    assert installer_path.stat().st_mode & 0o111
    assert "enable_service=0" in installer and "start_service=0" in installer
    assert "install -m 0640 -o root -g butters" in installer
    assert 'install -d -m 0750 -o root -g butters "${secret_dir}"' in installer
    assert "publish_agent_tree" in installer
    assert 'if [[ "${enable_service}" == "1" ]]' in installer
    assert "token_sha256" not in unit
    assert "command_key" not in unit
    assert "EnvironmentFile=" not in unit


def _installer_helper(
    body: str, *arguments: str | Path
) -> subprocess.CompletedProcess[str]:
    installer = Path(__file__).parents[1] / "scripts/install-agent-ingress"
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" || exit 90; shift; ' + body,
            "bash",
            str(installer),
            *(str(argument) for argument in arguments),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_normalizes_package_permissions(tmp_path: Path) -> None:
    tree = tmp_path / "staging"
    source = tree / "src/butters_agent/protocol.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    tree.chmod(0o700)
    source.parent.chmod(0o700)
    source.chmod(0o600)
    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    result = _installer_helper('normalize_agent_tree "$1" "$2" "$3"', tree, user, group)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(tree.stat().st_mode) == 0o750
    assert stat.S_IMODE(source.parent.stat().st_mode) == 0o750
    assert stat.S_IMODE(source.stat().st_mode) == 0o640


def test_installer_publish_failure_restores_previous_tree(tmp_path: Path) -> None:
    install = tmp_path / "butters-agent"
    previous = tmp_path / "butters-agent.previous"
    missing_staging = tmp_path / "missing-staging"
    install.mkdir()
    (install / "marker").write_text("current", encoding="utf-8")
    result = _installer_helper(
        'publish_agent_tree "$1" "$2" "$3"',
        missing_staging,
        install,
        previous,
    )
    assert result.returncode == 3
    assert (install / "marker").read_text(encoding="utf-8") == "current"


def test_machine_proxy_rejects_browser_and_non_agent_handshakes() -> None:
    raw = (
        "GET /agent/v1/session HTTP/1.1\r\n"
        "Host: butters\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: YWFhYWFhYWFhYWFhYWFhYQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    assert validate_handshake(raw).startswith(b"GET /agent/v1/session ")
    for extra in (
        b"Origin: https://browser.invalid\r\n",
        b"Cookie: butters_session=admin\r\n",
        b"Authorization: Bearer browser\r\n",
        b"Tailscale-User-Login: admin\r\n",
    ):
        with pytest.raises(ValueError, match="unexpected_header"):
            validate_handshake(raw.replace(b"\r\n\r\n", b"\r\n" + extra + b"\r\n"))
    with pytest.raises(ValueError, match="unknown_path"):
        validate_handshake(raw.replace(b"/agent/v1/session", b"/api/admin/tools"))


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8"])
def test_transport_refuses_wildcard_or_public_binding(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr("socket.gethostbyname_ex", lambda _host: ("x", [], [address]))
    with pytest.raises(ValueError, match="private_lan_binding_required"):
        bind_addresses({"bind_host": "agent.internal"})
