"""NAS Wake-on-LAN registration, privilege boundary, planner, and Admin UI."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from beta1_harness import admin_headers, build_settings, client
from butters.actions import broker_main
from butters.actions.broker import (
    BrokerError,
    BrokerOperation,
    FixedBrokerConfig,
    FixedBrokerOperations,
)
from butters.assistant_config import load_assistant_settings
from butters.planner.model import PlannerCatalogAction, PlannerError, PlannerRequest
from butters.planner.provider import DeterministicPlannerProvider
from butters.planner.validator import PlannerValidator
from butters.skills.model import AuthenticationLevel
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import (
    CONVERSATIONAL_PLANNER_ACTIONS,
    CONVERSATIONAL_PLANNER_PARAMETER_ENUMS,
    PLANNER_COMPOSITIONS,
    BetaAssistantService,
)

BUTTERS = Path(__file__).resolve().parents[1]
ADMIN_HTML = (BUTTERS / "src/butters/web/static/admin.html").read_text()
ADMIN_JS = (BUTTERS / "src/butters/web/static/assets/admin.js").read_text()
BROKER_CONFIG = BUTTERS / "config/action-broker.example.toml"


class NoCloud:
    available = False


def _nas_app(tmp_path: Path):
    settings = build_settings(tmp_path)
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings,
        vocabulary,
        general_reasoner=NoCloud(),
        state_dir=tmp_path,
    )
    return create_app(settings, vocabulary, service), service, settings


def _function(name: str) -> str:
    marker = f"function {name}("
    start = ADMIN_JS.index(marker)
    brace = ADMIN_JS.index("{", start)
    depth = 0
    for index in range(brace, len(ADMIN_JS)):
        if ADMIN_JS[index] == "{":
            depth += 1
        elif ADMIN_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return ADMIN_JS[start : index + 1]
    raise AssertionError(f"unbalanced function {name}")


def test_nas_wake_is_registered_as_an_empty_elevated_action(tmp_path: Path) -> None:
    settings = load_assistant_settings()
    assert settings.actions.nas.enabled is True
    assert settings.actions.nas.configured is True

    app_settings = settings
    # Registration is built by the normal application fixture below; keeping
    # this test synchronous avoids starting any worker or broker request.
    from butters.actions.store import ActionStateStore
    from butters.assistant import create_assistant
    state_path = tmp_path / "registration.sqlite3"
    try:
        assistant = create_assistant(
            app_settings,
            DomainVocabulary((), ()),
            action_state=ActionStateStore(state_path, settings.actions),
        )
        spec = assistant.skills.get("wake_nas")
        assert spec is not None
        assert spec.authentication is AuthenticationLevel.ELEVATED
        assert spec.input_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        assert spec.available is True
    finally:
        state_path.unlink(missing_ok=True)
        state_path.with_name(state_path.name + "-wal").unlink(missing_ok=True)
        state_path.with_name(state_path.name + "-shm").unlink(missing_ok=True)


def test_broker_selects_nas_operation_and_uses_only_trusted_configuration(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 0

    config = FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "desktop-key",
        nas_mac="00:e2:69:7d:40:cd",
        nas_broadcast="192.168.1.255",
        enabled_operations=frozenset(
            {BrokerOperation.DESKTOP_WAKE, BrokerOperation.NAS_WAKE}
        ),
    )
    handlers = FixedBrokerOperations(
        config,
        runner=lambda argv, **kwargs: calls.append((argv, kwargs)) or Result(),
    ).handlers()

    assert BrokerOperation.NAS_WAKE in handlers
    handlers[BrokerOperation.NAS_WAKE]()
    assert calls == [
        (
            [
                "/usr/bin/wakeonlan",
                "-i",
                "192.168.1.255",
                "00:e2:69:7d:40:cd",
            ],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "timeout": 10,
                "check": False,
            },
        )
    ]
    assert "shell" not in calls[0][1]

    handlers[BrokerOperation.DESKTOP_WAKE]()
    assert calls[1][0] == [
        "/usr/bin/wakeonlan",
        "-i",
        "192.168.1.255",
        "34:5A:60:D7:4C:2C",
    ]


def test_root_broker_parser_loads_the_fixed_nas_target(monkeypatch) -> None:
    monkeypatch.setattr(broker_main, "_require_root_private", lambda *_args: None)
    monkeypatch.setattr(
        broker_main.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=1234)
    )
    uid, config = broker_main._configuration(BROKER_CONFIG)
    assert uid == 1234
    assert config.nas_mac == "00:e2:69:7d:40:cd"
    assert config.nas_broadcast == "192.168.1.255"
    assert BrokerOperation.NAS_WAKE not in config.enabled_operations


@pytest.mark.parametrize(
    ("mac", "broadcast"),
    [
        ("not-a-mac", "192.168.1.255"),
        ("00:e2:69:7d:40:cd", "not-an-address"),
        ("00:e2:69:7d:40:cd", "127.0.0.1"),
    ],
)
def test_invalid_root_owned_wol_configuration_never_reaches_subprocess(
    tmp_path: Path, mac: str, broadcast: str
) -> None:
    calls = []
    config = FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "desktop-key",
        nas_mac=mac,
        nas_broadcast=broadcast,
        enabled_operations=frozenset({BrokerOperation.NAS_WAKE}),
    )
    operation = FixedBrokerOperations(
        config, runner=lambda *args, **kwargs: calls.append((args, kwargs))
    ).handlers()[BrokerOperation.NAS_WAKE]
    with pytest.raises(BrokerError) as error:
        operation()
    assert error.value.code == "invalid_configuration"
    assert calls == []


@pytest.mark.parametrize(
    "parameters",
    [
        {"mac": "aa:bb:cc:dd:ee:ff"},
        {"broadcast": "10.0.0.255"},
        {"ip": "192.168.1.2"},
        {"host": "nas.example"},
        {"interface": "enp6s0"},
        {"command": "wakeonlan"},
        {"shell": True},
        {"executable": "/tmp/wake"},
        {"arguments": ["-i", "10.0.0.255"]},
    ],
)
def test_planner_cannot_inject_machine_level_nas_parameters(
    tmp_path: Path, parameters: dict[str, object]
) -> None:
    app, service, _settings = _nas_app(tmp_path)
    try:
        validator = PlannerValidator(
            service.assistant.skills, compositions=PLANNER_COMPOSITIONS
        )
        catalog = validator.catalog(
            CONVERSATIONAL_PLANNER_ACTIONS,
            parameter_enums=CONVERSATIONAL_PLANNER_PARAMETER_ENUMS,
        )
        nas = next(item for item in catalog if item.action_id == "wake_nas")
        assert nas.input_schema["properties"] == {}
        with pytest.raises(PlannerError) as error:
            validator.validate(
                {
                    "summary": "Wake NAS.",
                    "rationale": "Use the registered action.",
                    "requires_confirmation": False,
                    "steps": [
                        {"action_id": "wake_nas", "parameters": parameters}
                    ],
                },
                catalog=catalog,
                administrator=True,
            )
        assert error.value.code in {"invalid_arguments", "policy_denied"}
    finally:
        asyncio.run(app.state.shutdown_workers())


def test_planner_catalog_exposes_only_high_level_nas_wake(tmp_path: Path) -> None:
    app, service, _settings = _nas_app(tmp_path)
    try:
        validator = PlannerValidator(service.assistant.skills)
        catalog = validator.catalog(CONVERSATIONAL_PLANNER_ACTIONS)
        entry = next(item for item in catalog if item.action_id == "wake_nas")
        assert entry.authentication == "elevated"
        assert entry.input_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        encoded = json.dumps(entry.safe_dict()).casefold()
        for forbidden in (
            "00:e2:69:7d:40:cd",
            "192.168.1.255",
            "enp6s0",
            "/usr/bin/wakeonlan",
        ):
            assert forbidden not in encoded
    finally:
        asyncio.run(app.state.shutdown_workers())


def test_deterministic_planner_proposes_nas_wake_without_parameters() -> None:
    catalog = (
        PlannerCatalogAction(
            "wake_nas",
            "Wake the configured NAS.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "action",
            "elevated",
            True,
        ),
    )
    proposal = DeterministicPlannerProvider().plan(
        PlannerRequest("wake my NAS", catalog, {}, ())
    )
    assert proposal["steps"] == [{"action_id": "wake_nas", "parameters": {}}]


def test_admin_tools_exposes_loading_success_and_error_behavior_without_targets() -> None:
    wake = _function("wakeNas")
    poll = _function("waitForAdminAction")
    assert 'id="wake-nas"' in ADMIN_HTML
    assert ">Wake NAS<" in ADMIN_HTML
    assert 'role="status"' in ADMIN_HTML
    assert "button.disabled=true" in wake
    assert 'button.textContent="Sending…"' in wake
    assert 'status.textContent="Wake packet sent."' in wake
    assert 'status.textContent=`Wake NAS failed:' in wake
    assert "button.disabled=false" in wake
    assert 'api("/api/admin/tools/wake-nas"' in wake
    assert "JSON.stringify({})" in wake
    assert 'job.state==="completed"' in poll
    for forbidden in (
        "00:e2:69:7d:40:cd",
        "192.168.1.255",
        "enp6s0",
        "/usr/bin/wakeonlan",
        "mac:",
        "broadcast:",
        "host:",
        "command:",
        "executable:",
    ):
        assert forbidden not in wake.casefold()


@pytest.mark.parametrize(
    "parameters",
    [
        {"mac": "aa:bb:cc:dd:ee:ff"},
        {"broadcast": "10.0.0.255"},
        {"ip": "192.168.1.2"},
        {"host": "nas.example"},
        {"command": "wakeonlan"},
        {"executable": "/tmp/wake"},
    ],
)
def test_admin_nas_endpoint_rejects_every_parameter(
    tmp_path: Path, parameters: dict[str, object]
) -> None:
    async def scenario() -> None:
        app, service, _settings = _nas_app(tmp_path)
        implementation = service.assistant.skills.get("wake_nas").implementation.__self__
        calls = []
        implementation.nas = SimpleNamespace(
            wake=lambda *_args: calls.append(True) or {"accepted": True}
        )
        try:
            async with client(app) as http:
                session = (
                    await http.get("/api/session", headers=admin_headers())
                ).json()
                response = await http.post(
                    "/api/admin/tools/wake-nas",
                    headers=admin_headers("http://testserver", session["csrf_token"]),
                    json=parameters,
                )
                assert response.status_code == 400
                assert response.json()["error"] == "invalid_request"
                assert calls == []
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_admin_nas_action_uses_elevation_coordinator_audit_and_broker_selection(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        app, service, settings = _nas_app(tmp_path)
        implementation = service.assistant.skills.get("wake_nas").implementation.__self__
        operations = []
        implementation.nas.actions = SimpleNamespace(
            execute=lambda operation, **_kwargs: operations.append(operation)
            or {"operation": operation.value, "accepted": True}
        )
        try:
            async with client(app) as http:
                created = await http.get("/api/session", headers=admin_headers())
                session_data = created.json()
                session_id = http.cookies.get("butters_session")
                assert session_id is not None
                session = service.sessions.require(session_id)
                mutation = admin_headers(
                    "http://testserver", session_data["csrf_token"]
                )

                pending = await http.post(
                    "/api/admin/tools/wake-nas", headers=mutation, json={}
                )
                assert pending.status_code == 200
                assert pending.json()["status"] == "authentication_required"
                assert pending.json()["pending_action"]["steps"] == [
                    {"skill": "wake_nas", "arguments": {}}
                ]
                assert operations == []

                service.auth_state.elevate(session.session_id, session.peer_key)
                queued = await http.post(
                    "/api/admin/tools/wake-nas", headers=mutation, json={}
                )
                assert queued.status_code == 200
                job_id = queued.json()["jobs"][0]["job_id"]
                for _ in range(100):
                    observed = await http.get(
                        f"/api/actions/jobs/{job_id}", headers=admin_headers()
                    )
                    if observed.json()["state"] in {"completed", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                assert observed.json()["state"] == "completed"
                assert operations == [BrokerOperation.NAS_WAKE]
                audits = service.action_state.audit_entries()
                completed = next(
                    item
                    for item in audits
                    if item["skill"] == "wake_nas" and item["outcome"] == "completed"
                )
                assert completed["arguments"] == {}
                assert completed["authentication"] == "elevated"
                assert settings.planner.enabled is False
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())
