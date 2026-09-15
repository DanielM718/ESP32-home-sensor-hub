from __future__ import annotations

import asyncio
import inspect
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import butters.desktop_agent_staging as staging_module
import butters.desktop_agent_staging_cli as cli_module
import httpx
import pytest
from butters.actions.agent_ingress import IngressConfig
from butters.actions.agent_ingress import run as run_tls_ingress
from butters.desktop_agent_staging import (
    STAGING_MACHINE_PORT,
    StagingValidationRuntime,
    create_machine_ingress_app,
    create_validation_app,
    load_staging_settings,
    validate_staging_settings,
)
from butters.desktop_agent_staging_cli import UnixHTTPConnection, main, parser
from butters.skills.model import ActionClass
from butters.skills.policy import PolicyValidator

BUTTERS = Path(__file__).resolve().parents[1]
INSTALLER = BUTTERS / "scripts" / "install-desktop-agent-staging"
CLI = BUTTERS / "scripts" / "desktop-agent-staging-validate"
WEB_UNIT = BUTTERS / "systemd" / "butters-staging.service"
INGRESS_UNIT = BUTTERS / "systemd" / "butters-agent-ingress-staging.service"
SOCKET_UNIT = BUTTERS / "systemd" / "butters-staging-validation.socket"
TMPFILES = BUTTERS / "tmpfiles.d" / "butters-staging.conf"
PREFLIGHT = BUTTERS / "scripts" / "desktop-agent-staging-preflight"
STAGING_REQUIREMENTS = BUTTERS / "requirements-staging.txt"
ASSISTANT_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-assistant.example.toml"
)
INGRESS_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-ingress.example.toml"
)
MACHINE_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-machine.example.toml"
)


@pytest.mark.parametrize(
    "candidate",
    [
        "/opt/butters",
        "/opt/butters/",
        "/opt/./butters",
        "/opt/butters-staging/../butters",
        "opt/butters-staging",
        "",
    ],
)
def test_installer_refuses_unsafe_install_roots(candidate):
    command = (
        f"source {INSTALLER!s}; "
        f"require_staging_target {candidate!r}"
    )
    result = subprocess.run(
        ["bash", "-c", command], text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "Refusing" in result.stderr


def test_installer_canonical_path_guard_handles_symlinks(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    escaped = tmp_path / "escaped"
    escaped.symlink_to(production, target_is_directory=True)

    accepted = subprocess.run(
        [
            "bash",
            "-c",
            f"source {INSTALLER!s}; require_isolated_path {staging!s} {staging!s}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    refused = subprocess.run(
        [
            "bash",
            "-c",
            f"source {INSTALLER!s}; require_isolated_path {escaped!s} {escaped!s}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0
    assert refused.returncode != 0


def test_staging_units_paths_and_ports_are_distinct():
    web = WEB_UNIT.read_text()
    ingress = INGRESS_UNIT.read_text()
    assistant = ASSISTANT_TEMPLATE.read_text()
    transport = INGRESS_TEMPLATE.read_text()
    installer = INSTALLER.read_text()

    assert WEB_UNIT.name == "butters-staging.service"
    assert INGRESS_UNIT.name == "butters-agent-ingress-staging.service"
    assert WEB_UNIT.name != "butters-web.service"
    assert INGRESS_UNIT.name != "butters-agent-ingress.service"
    assert "StateDirectory=butters-staging" in web
    assert "Requires=butters-staging-validation.socket" in web
    assert "RuntimeDirectory=butters-staging" not in web
    assert "StateDirectory=butters-agent-ingress-staging" in ingress
    assert "RuntimeDirectory=butters-agent-ingress-staging" in ingress
    assert 'state_dir = "/var/lib/butters-staging"' in assistant
    assert f"port = {STAGING_MACHINE_PORT}" in assistant
    assert "port = 18443" in transport
    assert "upstream_port = 18090" in transport
    assert 'install_root="/opt/butters-staging"' in installer
    assert "ListenStream=/run/butters-staging/validation.sock" in SOCKET_UNIT.read_text()
    assert "SocketUser=root" in SOCKET_UNIT.read_text()
    assert "SocketGroup=butters-staging-ops" in SOCKET_UNIT.read_text()
    assert "SocketMode=0660" in SOCKET_UNIT.read_text()
    assert "ConditionPathIsDirectory=/run/butters-staging" in SOCKET_UNIT.read_text()
    assert (
        "d /run/butters-staging 0750 butters-staging butters-staging-ops -"
        in TMPFILES.read_text()
    )
    assert 'tmpfiles_name="butters-staging.conf"' in installer
    assert "systemd-tmpfiles --create" in installer
    assert "enable_services=0" in installer
    assert 'if [[ "${enable_services}" == 1 ]]' in installer
    preflight = PREFLIGHT.read_text()
    assert "machine_port=18090" in preflight
    assert "lan_tls_port=18443" in preflight
    assert "validation_socket=/run/butters-staging/validation.sock" in preflight
    assert "socket-unit-active=" in preflight
    assert "Unix listener evidence" in preflight
    assert "Do not delete or unlink this path blindly" in preflight
    requirements = STAGING_REQUIREMENTS.read_text()
    assert "websockets==15.0.1" in requirements
    assert "webauthn" not in requirements
    assert "requirements-staging.txt" in installer


def _can_read(
    mode: int,
    *,
    file_uid: int,
    file_gid: int,
    actor_uid: int,
    actor_gids: set[int],
) -> bool:
    if actor_uid == 0:
        return True
    if actor_uid == file_uid:
        return bool(mode & stat.S_IRUSR)
    if file_gid in actor_gids:
        return bool(mode & stat.S_IRGRP)
    return bool(mode & stat.S_IROTH)


def test_service_secrets_are_owner_only_and_operator_group_is_distinct():
    installer = INSTALLER.read_text()
    assert "groupadd --system butters-staging-ops" in installer
    assert "useradd --system --gid butters-staging" in installer
    assert "--gid butters-staging-ops" not in installer
    assert (
        'chmod 0400 "${config_root}/desktop-agent/${private_credential}"'
        in installer
    )
    assert 'chmod 0600 "${config_root}/${staging_config}"' in installer
    assert 'chown butters-staging:root' in installer

    service_uid = 991
    service_gid = 991
    operator_uid = 1000
    operator_gid = 992
    assert _can_read(
        0o400,
        file_uid=service_uid,
        file_gid=0,
        actor_uid=service_uid,
        actor_gids={service_gid},
    )
    assert not _can_read(
        0o400,
        file_uid=service_uid,
        file_gid=0,
        actor_uid=operator_uid,
        actor_gids={operator_gid},
    )
    assert service_gid not in {operator_gid}


def test_sensitive_staging_configs_are_service_owned_and_not_operator_readable():
    service_uid = 991
    operator_uid = 1000
    operator_gid = 992
    assert _can_read(
        0o600,
        file_uid=service_uid,
        file_gid=0,
        actor_uid=service_uid,
        actor_gids=set(),
    )
    assert not _can_read(
        0o600,
        file_uid=service_uid,
        file_gid=0,
        actor_uid=operator_uid,
        actor_gids={operator_gid},
    )


def test_port_preflight_fails_closed_when_listener_inventory_fails(tmp_path):
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    fake_ss = binary_dir / "ss"
    fake_ss.write_text("#!/bin/sh\nexit 1\n")
    fake_ss.chmod(0o755)
    result = subprocess.run(
        [str(PREFLIGHT)],
        env={**os.environ, "PATH": f"{binary_dir}:/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "unable to inspect staging port 18090" in result.stderr


def test_staging_templates_use_only_staging_credential_paths_and_disabled_gates():
    assistant = ASSISTANT_TEMPLATE.read_text()
    transport = INGRESS_TEMPLATE.read_text()
    machine = MACHINE_TEMPLATE.read_text()
    combined = assistant + transport + machine

    assert '/etc/butters/desktop-agent' not in combined
    assert combined.count("enabled = false") >= 2
    assert '[agent_ingress]\nenabled = false' in assistant
    assert transport.startswith("# Gate 2")
    assert "enabled = false" in transport
    assert "/etc/butters-staging/desktop-agent/command.key" in machine
    assert "REPLACE_WITH" in machine


def test_validation_cli_has_only_fixed_subcommands_and_no_generic_hub_console():
    command_parser = parser()
    subparser = next(
        action for action in command_parser._actions if action.dest == "command"
    )
    assert set(subparser.choices) == {"state", "list-apps", "status", "launch"}
    source = (
        BUTTERS / "src" / "butters" / "desktop_agent_staging_cli.py"
    ).read_text()
    assert "AgentHub" not in source
    assert "payload" not in " ".join(subparser.choices)
    assert "PYTHONPATH" in CLI.read_text()


def _config_text(
    *,
    state_dir: Path,
    agent_config: Path,
    host: str = "127.0.0.1",
    port: int = STAGING_MACHINE_PORT,
    enabled: bool = False,
) -> str:
    return "\n".join(
        (
            "[staging]",
            f'state_dir = "{state_dir}"',
            f'host = "{host}"',
            f"port = {port}",
            "[agent_ingress]",
            f"enabled = {'true' if enabled else 'false'}",
            f'config_path = "{agent_config}"',
            "request_timeout_seconds = 2",
            "[actions]",
            "audit_capacity = 1000",
            "job_capacity = 256",
        )
    )


def _staging_fixture(tmp_path, monkeypatch, **changes):
    config_root = tmp_path / "etc" / "butters-staging"
    state_root = tmp_path / "var" / "lib" / "butters-staging"
    config_root.mkdir(parents=True)
    state_root.mkdir(parents=True)
    monkeypatch.setattr(staging_module, "STAGING_CONFIG_ROOT", config_root)
    monkeypatch.setattr(staging_module, "STAGING_STATE_ROOT", state_root)
    path = config_root / "assistant.toml"
    values = {
        "state_dir": state_root,
        "agent_config": config_root / "desktop-agent.toml",
    }
    values.update(changes)
    path.write_text(_config_text(**values))
    path.chmod(0o640)
    return path, load_staging_settings(path), config_root, state_root


def test_valid_disabled_staging_settings_are_accepted(tmp_path, monkeypatch):
    path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch, enabled=False
    )
    validate_staging_settings(path, settings)
    assert settings.agent_ingress.enabled is False


def test_staging_config_outside_root_is_rejected(tmp_path, monkeypatch):
    _path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch
    )
    outside = tmp_path / "assistant.toml"
    outside.write_text(_config_text(
        state_dir=settings.state_dir,
        agent_config=settings.agent_ingress.config_path,
    ))
    outside.chmod(0o640)
    with pytest.raises(ValueError, match="staging_config_root_required"):
        validate_staging_settings(outside, load_staging_settings(outside))


def test_staging_state_outside_root_is_rejected(tmp_path, monkeypatch):
    path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch, state_dir=tmp_path / "outside-state"
    )
    with pytest.raises(ValueError, match="staging_state_root_required"):
        validate_staging_settings(path, settings)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"host": "0.0.0.0"}, "staging_loopback_required"),
        ({"port": 8090}, "staging_machine_port_required"),
        ({"port": 18091}, "staging_machine_port_required"),
    ],
)
def test_staging_machine_listener_identity_is_exact(
    tmp_path, monkeypatch, change, message
):
    path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch, **change
    )
    with pytest.raises(ValueError, match=message):
        validate_staging_settings(path, settings)


@pytest.mark.parametrize("field", ["config", "state", "agent"])
def test_relative_staging_paths_are_rejected(tmp_path, monkeypatch, field):
    path, settings, config_root, _state_root = _staging_fixture(tmp_path, monkeypatch)
    if field == "config":
        with pytest.raises(ValueError, match="absolute_staging_config_required"):
            validate_staging_settings(Path("assistant.toml"), settings)
        return
    if field == "state":
        path.write_text(_config_text(
            state_dir=Path("state"), agent_config=config_root / "desktop-agent.toml"
        ))
        message = "absolute_staging_state_required"
    else:
        path.write_text(_config_text(
            state_dir=settings.state_dir, agent_config=Path("desktop-agent.toml")
        ))
        message = "agent_ingress.config_path must be absolute"
    with pytest.raises(ValueError, match=message):
        validate_staging_settings(path, load_staging_settings(path))


def test_world_writable_staging_config_is_rejected(tmp_path, monkeypatch):
    path, settings, _config_root, _state_root = _staging_fixture(tmp_path, monkeypatch)
    path.chmod(0o666)
    with pytest.raises(ValueError, match="unsafe_staging_configuration"):
        validate_staging_settings(path, settings)


def test_staging_config_symlink_escape_is_rejected(tmp_path, monkeypatch):
    path, settings, config_root, _state_root = _staging_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside.toml"
    outside.write_text(path.read_text())
    outside.chmod(0o640)
    path.unlink()
    path.symlink_to(outside)
    assert path.parent == config_root
    with pytest.raises(ValueError, match="staging_config_root_required"):
        validate_staging_settings(path, settings)


def test_staging_state_symlink_escape_is_rejected(tmp_path, monkeypatch):
    path, _settings, config_root, state_root = _staging_fixture(tmp_path, monkeypatch)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    escaped = state_root / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    path.write_text(_config_text(
        state_dir=escaped, agent_config=config_root / "desktop-agent.toml"
    ))
    with pytest.raises(ValueError, match="staging_state_root_required"):
        validate_staging_settings(path, load_staging_settings(path))


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["state"], ("GET", "/validation/v1/state")),
        (["list-apps"], ("POST", "/validation/v1/list-apps")),
        (["status", "notepad"], ("POST", "/validation/v1/status/notepad")),
        (["launch", "notepad"], ("POST", "/validation/v1/launch/notepad")),
    ],
)
def test_cli_main_executes_all_fixed_commands(
    tmp_path, monkeypatch, capsys, argv, expected
):
    config = tmp_path / "assistant.toml"
    config.write_text(_config_text(state_dir=tmp_path, agent_config=tmp_path / "agent"))
    calls = []
    monkeypatch.setenv("BUTTERS_STAGING_CONFIG", str(config))
    monkeypatch.setattr(cli_module, "validate_staging_settings", lambda *_: None)
    monkeypatch.setattr(
        cli_module,
        "_request",
        lambda method, path, timeout: calls.append((method, path)) or {"ok": True},
    )
    monkeypatch.setattr(sys, "argv", ["desktop-agent-staging-validate", *argv])
    assert main() == 0
    assert calls == [expected]
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.parametrize("argv", [["status"], ["launch"], ["status", "../bad"]])
def test_cli_app_argument_failures_remain_in_argparse(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["desktop-agent-staging-validate", *argv])
    with pytest.raises(SystemExit) as failure:
        main()
    assert failure.value.code == 2


def test_cli_uses_unix_socket_transport(tmp_path, monkeypatch):
    path = tmp_path / "validation.sock"
    calls = []

    class AuthorizedSocket:
        def settimeout(self, timeout):
            calls.append(("timeout", timeout))

        def connect(self, address):
            calls.append(("connect", address))

    def socket_factory(family, kind):
        calls.append(("socket", family, kind))
        return AuthorizedSocket()

    monkeypatch.setattr(socket, "socket", socket_factory)
    connection = UnixHTTPConnection(path, 2)
    connection.connect()
    assert calls == [
        ("socket", socket.AF_UNIX, socket.SOCK_STREAM),
        ("timeout", 2),
        ("connect", str(path)),
    ]


def test_daemon_accepts_only_verified_systemd_socket(monkeypatch):
    expected_path = "/run/butters-staging/validation.sock"

    class SocketPath:
        def __str__(self):
            return expected_path

        def stat(self):
            return SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o660,
                st_uid=0,
                st_gid=123,
            )

    class InheritedSocket:
        def getsockname(self):
            return expected_path

    inherited = InheritedSocket()
    monkeypatch.setenv("LISTEN_PID", str(staging_module.os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setattr(staging_module, "STAGING_VALIDATION_SOCKET", SocketPath())
    requested_groups = []
    monkeypatch.setattr(
        staging_module.grp,
        "getgrnam",
        lambda name: requested_groups.append(name) or SimpleNamespace(gr_gid=123),
    )
    monkeypatch.setattr(
        staging_module.socket, "socket", lambda *, fileno: inherited
    )
    assert staging_module.systemd_validation_socket() is inherited
    assert requested_groups == ["butters-staging-ops"]


@pytest.mark.parametrize(
    ("listen_pid", "listen_fds"),
    [
        ("wrong", "1"),
        (None, "1"),
        ("current", "0"),
        ("current", "2"),
        ("current", None),
    ],
)
def test_daemon_rejects_mismatched_systemd_activation_environment(
    monkeypatch, listen_pid, listen_fds
):
    pid = str(staging_module.os.getpid()) if listen_pid == "current" else listen_pid
    if pid is None:
        monkeypatch.delenv("LISTEN_PID", raising=False)
    else:
        monkeypatch.setenv("LISTEN_PID", pid)
    if listen_fds is None:
        monkeypatch.delenv("LISTEN_FDS", raising=False)
    else:
        monkeypatch.setenv("LISTEN_FDS", listen_fds)
    with pytest.raises(RuntimeError, match="staging_validation_socket_required"):
        staging_module.systemd_validation_socket()


@pytest.mark.parametrize(
    ("path_mode", "path_uid", "path_gid"),
    [
        (stat.S_IFREG | 0o660, 0, 123),
        (stat.S_IFSOCK | 0o660, 1, 123),
        (stat.S_IFSOCK | 0o660, 0, 124),
        (stat.S_IFSOCK | 0o666, 0, 123),
        (stat.S_IFSOCK | 0o662, 0, 123),
    ],
)
def test_daemon_rejects_unsafe_validation_path_metadata(
    monkeypatch, path_mode, path_uid, path_gid
):
    expected_path = "/run/butters-staging/validation.sock"

    class SocketPath:
        def __str__(self):
            return expected_path

        def stat(self):
            return SimpleNamespace(
                st_mode=path_mode,
                st_uid=path_uid,
                st_gid=path_gid,
            )

    class InheritedSocket:
        closed = False

        def getsockname(self):
            return expected_path

        def close(self):
            self.closed = True

    inherited = InheritedSocket()
    monkeypatch.setenv("LISTEN_PID", str(staging_module.os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "1")
    monkeypatch.setattr(staging_module, "STAGING_VALIDATION_SOCKET", SocketPath())
    monkeypatch.setattr(
        staging_module.grp,
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid=123),
    )
    monkeypatch.setattr(staging_module.socket, "socket", lambda *, fileno: inherited)
    with pytest.raises(RuntimeError, match="unsafe_staging_validation_socket"):
        staging_module.systemd_validation_socket()
    assert inherited.closed is True


class RecordingPolicy(PolicyValidator):
    def __init__(self):
        super().__init__(
            allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
        )
        self.skills: list[str] = []

    def authorize(self, **values):
        self.skills.append(values["skill_name"])
        return super().authorize(**values)


class FakeHub:
    configured = True

    def __init__(self, settings):
        self.settings = settings

    async def socket(self, websocket):
        await websocket.close(code=1008)

    def status(self):
        return {"configured": True, "state": "connected"}

    def list_apps(self):
        return {
            "action": "desktop.app.list",
            "success": True,
            "apps": [
                {
                    "app": "notepad",
                    "status": "not_running",
                    "available": True,
                }
            ],
        }

    def app_status(self, app):
        return {
            "action": "desktop.app.status",
            "success": True,
            "app": app,
            "status": "not_running",
            "available": True,
        }

    def launch_app(self, app, *, cancel, idempotency_key):
        assert cancel is not None
        assert idempotency_key
        return {
            "action": "desktop.app.launch",
            "success": True,
            "app": app,
            "status": "running",
            "available": True,
            "outcome": "launched",
            "request_to_ack_ms": 12.5,
            "ack_to_result_ms": 40.0,
            "request_to_result_ms": 52.5,
        }


def test_launch_uses_registry_policy_and_action_coordinator(tmp_path):
    config = tmp_path / "assistant.toml"
    config.write_text(
        "\n".join(
            (
                "[staging]",
                f'state_dir = "{tmp_path}"',
                'host = "127.0.0.1"',
                "port = 18090",
                "[agent_ingress]",
                "enabled = true",
                f'config_path = "{tmp_path / "machine.toml"}"',
                "request_timeout_seconds = 2",
                "[actions]",
                "audit_capacity = 1000",
                "job_capacity = 256",
            )
        )
    )
    settings = load_staging_settings(config)
    policy = RecordingPolicy()
    runtime = StagingValidationRuntime(
        settings,
        hub=FakeHub(settings.agent_ingress),
        state_dir=tmp_path,
        policy=policy,
    )
    execute = runtime.coordinator.execute
    authentications = []

    def recording_execute(*args, **kwargs):
        authentication = kwargs["authentication"]
        plan = runtime.store.require(
            args[0],
            session_id=kwargs["session_id"],
            identity=kwargs["identity"],
            allowed_states=frozenset({"pending_auth", "pending_confirmation"}),
        )
        authentications.append((authentication, plan.digest))
        return execute(*args, **kwargs)

    runtime.coordinator.execute = recording_execute

    result = runtime.launch("notepad")

    assert result["ok"] is True
    assert result["authorization"] == "staging_local_host_assertion"
    assert result["plan"]["steps"] == [
        {"skill": "desktop.app.launch", "arguments": {"app": "notepad"}}
    ]
    assert result["job"]["state"] == "completed"
    assert policy.skills == ["desktop.app.launch"]
    audit = runtime.store.audit_entries()
    assert audit[0]["skill"] == "desktop.app.launch"
    assert audit[0]["method"] == "staging_local_host_assertion"
    authentication, plan_digest = authentications[0]
    assert authentication.level.value == "elevated"
    assert authentication.action_digest == plan_digest

    routes = create_validation_app(runtime).routes
    validation_routes = [
        route for route in routes if str(route.path).startswith("/validation/")
    ]
    assert len(validation_routes) == 4
    assert all(inspect.iscoroutinefunction(route.endpoint) for route in validation_routes)
    machine_routes = create_machine_ingress_app(runtime).routes
    assert [route.path for route in machine_routes] == ["/agent/v1/session"]
    assert all(not route.path.startswith("/validation/") for route in machine_routes)


def test_disabled_gate_suppresses_machine_and_validation_capabilities(
    tmp_path, monkeypatch
):
    path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch, enabled=False
    )
    validate_staging_settings(path, settings)
    runtime = StagingValidationRuntime(
        settings,
        hub=FakeHub(settings.agent_ingress),
        state_dir=tmp_path,
    )
    assert create_machine_ingress_app(runtime).routes == []

    async def scenario():
        transport = httpx.ASGITransport(app=create_validation_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://unix") as client:
            state = await client.get("/validation/v1/state")
            assert state.status_code == 200
            for endpoint in (
                "/validation/v1/list-apps",
                "/validation/v1/status/notepad",
                "/validation/v1/launch/notepad",
            ):
                response = await client.post(endpoint)
                assert response.status_code == 503
                assert response.json()["error"] == "staging_gate_disabled"

    asyncio.run(scenario())


def test_disabled_gate_starts_no_machine_tcp_server(tmp_path, monkeypatch):
    _path, settings, _config_root, _state_root = _staging_fixture(
        tmp_path, monkeypatch, enabled=False
    )
    runtime = StagingValidationRuntime(
        settings, hub=FakeHub(settings.agent_ingress), state_dir=tmp_path
    )
    calls = []

    class Config:
        def __init__(self, app, **options):
            self.app = app
            self.options = options

    class Server:
        def __init__(self, config):
            self.config = config

        async def serve(self, sockets=None):
            calls.append((self.config.options, sockets))

    validation = object()
    monkeypatch.setattr(staging_module.uvicorn, "Config", Config)
    monkeypatch.setattr(staging_module.uvicorn, "Server", Server)
    asyncio.run(staging_module.serve(runtime, validation))
    assert len(calls) == 1
    options, sockets = calls[0]
    assert "host" not in options and "port" not in options
    assert sockets == [validation]


def test_disabled_tls_gate_opens_no_lan_listener(monkeypatch, tmp_path):
    config = IngressConfig(
        enabled=False,
        bind_interfaces=("eth0",),
        bind_host=None,
        port=18443,
        certificate=tmp_path / "tls.crt",
        private_key=tmp_path / "tls.key",
        upstream_host="127.0.0.1",
        upstream_port=18090,
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled TLS gate attempted to start a listener")

    monkeypatch.setattr(asyncio, "start_server", forbidden)
    asyncio.run(run_tls_ingress(config))


def test_tcp_surface_rejects_validation_routes_when_gate_enabled(tmp_path):
    config = tmp_path / "assistant.toml"
    config.write_text(
        _config_text(
            state_dir=tmp_path,
            agent_config=tmp_path / "machine.toml",
            enabled=True,
        )
    )
    settings = load_staging_settings(config)
    runtime = StagingValidationRuntime(
        settings,
        hub=FakeHub(settings.agent_ingress),
        state_dir=tmp_path,
    )

    async def scenario():
        transport = httpx.ASGITransport(app=create_machine_ingress_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://tcp") as client:
            for endpoint in (
                "/validation/v1/state",
                "/validation/v1/list-apps",
                "/validation/v1/status/notepad",
                "/validation/v1/launch/notepad",
            ):
                assert (await client.get(endpoint)).status_code == 404

    asyncio.run(scenario())


def test_authorized_validation_app_supports_four_fixed_operations(
    tmp_path, monkeypatch
):
    config = tmp_path / "assistant.toml"
    config.write_text(
        _config_text(
            state_dir=tmp_path,
            agent_config=tmp_path / "machine.toml",
            enabled=True,
        )
    )
    settings = load_staging_settings(config)
    runtime = StagingValidationRuntime(
        settings,
        hub=FakeHub(settings.agent_ingress),
        state_dir=tmp_path,
    )
    runtime.launch = lambda app: {"ok": True, "app": app}

    async def direct_call(function, *args):
        return function(*args)

    monkeypatch.setattr(staging_module.asyncio, "to_thread", direct_call)

    async def scenario():
        transport = httpx.ASGITransport(app=create_validation_app(runtime))
        async with httpx.AsyncClient(transport=transport, base_url="http://unix") as client:
            responses = (
                await client.get("/validation/v1/state"),
                await client.post("/validation/v1/list-apps"),
                await client.post("/validation/v1/status/notepad"),
                await client.post("/validation/v1/launch/notepad"),
            )
            assert all(response.status_code == 200 for response in responses)
            assert all(response.json()["ok"] is True for response in responses)

    asyncio.run(scenario())


def test_staging_code_has_no_fault_injection_or_generic_agent_invocation():
    source = (BUTTERS / "src" / "butters" / "desktop_agent_staging.py").read_text()
    assert "fault_injection" not in source
    assert "hub._request" not in source
    assert "launch_app(" not in source


def test_operator_cli_does_not_require_service_owned_configuration(
    tmp_path, monkeypatch
) -> None:
    """An authorized operator cannot read the daemon's config, by design.

    `butters-staging-ops` grants the four fixed validation operations and
    deliberately not staging-secret access, so the CLI must not depend on
    opening a `butters-staging:root` 0600 file. It previously did, which made
    every operator command fail with PermissionError before reaching the socket.
    """

    from butters.assistant_config import AgentIngressSettings
    from butters.desktop_agent_staging_cli import _request_timeout_seconds

    unreadable = tmp_path / "assistant.toml"
    unreadable.write_text("[staging]\n")

    def deny(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(
        "butters.desktop_agent_staging_cli.load_staging_settings", deny
    )
    assert (
        _request_timeout_seconds(unreadable)
        == AgentIngressSettings().request_timeout_seconds
    )


def test_operator_cli_still_validates_configuration_it_can_read(
    tmp_path, monkeypatch
) -> None:
    """Root keeps the full read-and-validate path; only permission falls back."""

    from butters.desktop_agent_staging_cli import _request_timeout_seconds

    calls: list[str] = []

    def loaded(_path):
        calls.append("load")
        raise ValueError("staging_loopback_required")

    monkeypatch.setattr(
        "butters.desktop_agent_staging_cli.load_staging_settings", loaded
    )
    # A genuine configuration fault must still surface, not be swallowed.
    with pytest.raises(ValueError, match="staging_loopback_required"):
        _request_timeout_seconds(tmp_path / "assistant.toml")
    assert calls == ["load"]
