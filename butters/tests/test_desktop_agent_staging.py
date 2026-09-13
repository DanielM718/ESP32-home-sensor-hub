from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

from butters.desktop_agent_staging import (
    StagingValidationRuntime,
    create_app,
    load_staging_settings,
)
from butters.desktop_agent_staging_cli import parser
from butters.skills.model import ActionClass
from butters.skills.policy import PolicyValidator

BUTTERS = Path(__file__).resolve().parents[1]
INSTALLER = BUTTERS / "scripts" / "install-desktop-agent-staging"
CLI = BUTTERS / "scripts" / "desktop-agent-staging-validate"
WEB_UNIT = BUTTERS / "systemd" / "butters-staging.service"
INGRESS_UNIT = BUTTERS / "systemd" / "butters-agent-ingress-staging.service"
ASSISTANT_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-assistant.example.toml"
)
INGRESS_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-ingress.example.toml"
)
MACHINE_TEMPLATE = (
    BUTTERS / "config" / "desktop-agent-staging-machine.example.toml"
)


def test_installer_refuses_production_install_root():
    command = (
        f"source {INSTALLER!s}; "
        "require_staging_target /opt/butters"
    )
    result = subprocess.run(
        ["bash", "-c", command], text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "Refusing" in result.stderr


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
    assert "RuntimeDirectory=butters-staging" in web
    assert "StateDirectory=butters-agent-ingress-staging" in ingress
    assert "RuntimeDirectory=butters-agent-ingress-staging" in ingress
    assert 'state_dir = "/var/lib/butters-staging"' in assistant
    assert "port = 18090" in assistant
    assert "port = 18443" in transport
    assert "upstream_port = 18090" in transport
    assert 'install_root="/opt/butters-staging"' in installer


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

    routes = create_app(runtime).routes
    validation_routes = [
        route for route in routes if str(route.path).startswith("/validation/")
    ]
    assert len(validation_routes) == 4
    assert all(inspect.iscoroutinefunction(route.endpoint) for route in validation_routes)


def test_staging_code_has_no_fault_injection_or_generic_agent_invocation():
    source = (BUTTERS / "src" / "butters" / "desktop_agent_staging.py").read_text()
    assert "fault_injection" not in source
    assert "hub._request" not in source
    assert "launch_app(" not in source
