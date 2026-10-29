"""KR260 JTAG recovery through the Desktop Agent: Butters-side guards.

Nothing here reaches a cable: the agent hub is a fake, and the agent's own
refusals are tested in butters-agent/tests/test_kr260.py.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_kr260_management import (
    TELEMETRY,
    Actions,
    Broker,
    Monotonic,
    Runner,
    settings,
)

from butters.actions.agent import AgentHub
from butters.assistant_config import AgentIngressSettings, load_assistant_settings
from butters.config_overlay import GATE_KEYS
from butters.integrations import kr260_jtag
from butters.integrations.actions import Kr260Adapter
from butters.integrations.kr260 import Kr260Observer
from butters.integrations.model import IntegrationError

NEW_BOOT = TELEMETRY.replace(
    "boot_id=63bb230d-e96c-438c-917a-dcc464a80a44",
    "boot_id=329affab-9e29-4709-a3f0-f63080f12afa",
).replace("uptime=10860.37", "uptime=80.00")
CHECK = "kr260.jtag_por.check"
RESET = "kr260.jtag_por.reset"


class Hub:
    """Stands in for AgentHub.kr260_jtag; records every call."""

    configured = True

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def kr260_jtag(self, action, *, cancel=None, idempotency_key=None):
        self.calls.append((action, idempotency_key))
        if self.result is not None:
            return {"action": action, **self.result}
        if action == CHECK:
            return {"action": action, "success": True, "jtag_result": "CHECK_OK"}
        return {
            "action": action,
            "success": True,
            "jtag_result": "RST_ISSUED",
            "reset_may_have_run": True,
            "reset_issued_at": "2026-10-29T03:26:40.012Z",
        }


def _adapter(*, broker=None, hub=None, enabled=True, ping=1):
    clock = Monotonic()
    base = load_assistant_settings()
    gate = replace(
        base.actions.kr260_jtag_recovery, enabled=enabled, configured=enabled
    )
    reboot = replace(base.actions.kr260_reboot, enabled=True, configured=True)
    broker = broker or Broker(ok=False, error_code="host_unreachable")
    observer = Kr260Observer(
        settings(None),
        broker=broker,
        runner=Runner(ping=ping),
        clock=clock,
        monotonic=clock,
    )
    adapter = Kr260Adapter(
        observer, Actions(), reboot, jtag_settings=gate, monotonic=clock
    )
    hub = Hub() if hub is None else hub
    adapter.attach_jtag(hub)
    return adapter, hub, broker, clock


def _dark_for(adapter, clock, seconds):
    """Observe the board dark now and again ``seconds`` later."""

    adapter.status(refresh=True)
    clock.now += seconds
    adapter.status(refresh=True)


# ----- the policy ------------------------------------------------------------


def _offline(**overrides):
    value = {
        "operational_state": "offline",
        "network_reachable": False,
        "ssh_available": False,
        "identity_verified": False,
        "error": "host_unreachable",
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "observation, dark",
    [
        (_offline(), True),
        (_offline(error="ssh_timeout"), True),
        # The broker is down, not the board.
        (_offline(error="ssh_unavailable"), False),
        (_offline(error="capability_disabled"), False),
        # Something answered.
        (_offline(network_reachable=True), False),
        (_offline(error="ssh_auth_failed"), False),
        (
            _offline(operational_state="identity_failed", error="identity_mismatch"),
            False,
        ),
        ({"operational_state": "online", "identity_verified": True}, False),
        ({}, False),
    ],
)
def test_only_a_silent_board_counts_as_dark(observation, dark):
    assert kr260_jtag.is_dark(observation) is dark


def test_a_single_answer_ends_the_dark_run():
    run = kr260_jtag.DarkRun()
    run.record(_offline(), 0.0)
    run.record(_offline(), 200.0)
    run.record(_offline(network_reachable=True), 250.0)
    run.record(_offline(), 400.0)
    assert (run.since, run.count) == (400.0, 1)


@pytest.mark.parametrize(
    "count, seconds, last_kexec, last_reset, reset, expected",
    [
        (0, 0, None, None, True, "board_reachable"),
        (1, 0, None, None, True, "offline_not_established"),
        (2, 299, None, None, True, "offline_not_established"),
        (2, 300, None, None, True, None),
        (2, 300, None, None, False, None),
        (2, 900, 500.0, None, True, "restart_recent"),
        (2, 900, 700.0, None, True, None),
        (2, 900, None, 100.0, True, "reset_rate_limited"),
        # A check is never rate limited by a reset.
        (2, 900, None, 100.0, False, None),
    ],
)
def test_refusal_rules(count, seconds, last_kexec, last_reset, reset, expected):
    now = 1000.0
    run = kr260_jtag.DarkRun()
    if count:
        run.since, run.count = now - seconds, count
    assert (
        kr260_jtag.refusal(
            now=now,
            dark=run,
            last_kexec_at=None if last_kexec is None else now - last_kexec,
            last_reset_at=None if last_reset is None else now - last_reset,
            reset=reset,
        )
        == expected
    )


def test_policy_constants_are_the_documented_ones():
    assert kr260_jtag.DARK_SECONDS == 300.0
    assert kr260_jtag.KEXEC_QUIET_SECONDS == 600.0
    assert kr260_jtag.POR_INTERVAL_SECONDS == 1800.0


@pytest.mark.parametrize(
    "observation, previous, age, expected",
    [
        ({"identity_verified": True, "boot_id": "b"}, "a", 80.0, (True, "confirmed")),
        ({"identity_verified": True, "boot_id": "a"}, "a", 80.0, (False, "requested")),
        (
            {"identity_verified": True, "boot_id": "a"},
            "a",
            400.0,
            (False, "unconfirmed"),
        ),
        # Unknown previous boot: an uptime shorter than the request's age.
        (
            {"identity_verified": True, "uptime_seconds": 70.0},
            None,
            80.0,
            (True, "confirmed"),
        ),
        (
            {"identity_verified": True, "uptime_seconds": 9000.0},
            None,
            80.0,
            (False, "requested"),
        ),
        # Never confirmed without a verified identity.
        ({"identity_verified": False, "boot_id": "b"}, "a", 80.0, (None, "requested")),
        ({"identity_verified": False}, None, 400.0, (None, "unconfirmed")),
    ],
)
def test_reset_state(observation, previous, age, expected):
    assert (
        kr260_jtag.reset_state(observation, age_seconds=age, previous_boot_id=previous)
        == expected
    )


# ----- the adapter: gates ------------------------------------------------------


def test_gate_ships_closed_and_is_a_machine_owned_overlay_key():
    base = load_assistant_settings()
    assert base.actions.kr260_jtag_recovery.enabled is False
    assert base.actions.kr260_jtag_recovery.configured is False
    for flag in ("enabled", "configured", "local_console_allowed"):
        assert f"actions.kr260_jtag_recovery.{flag}" in GATE_KEYS


@pytest.mark.parametrize("operation", ["jtag_check", "jtag_reset"])
def test_closed_gate_takes_no_probe_and_no_agent_call(operation):
    adapter, hub, broker, _clock = _adapter(enabled=False)
    with pytest.raises(IntegrationError) as error:
        getattr(adapter, operation)()
    assert error.value.code == "capability_unavailable"
    assert broker.calls == [] and hub.calls == []
    assert adapter.status()["capability"]["jtag_reset"] is False


def test_unattached_or_unconfigured_hub_is_unavailable():
    adapter, _hub, _broker, _clock = _adapter()
    adapter.attach_jtag(None)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "capability_unavailable"

    unconfigured = Hub()
    unconfigured.configured = False
    adapter.attach_jtag(unconfigured)
    with pytest.raises(IntegrationError):
        adapter.jtag_check()
    assert unconfigured.calls == []


def test_open_gate_reports_the_capability():
    adapter, _hub, _broker, _clock = _adapter()
    assert adapter.status()["capability"]["jtag_reset"] is True


# ----- the adapter: never on a board Butters can reach ----------------------------


@pytest.mark.parametrize("operation", ["jtag_check", "jtag_reset"])
def test_reachable_board_is_refused(operation):
    adapter, hub, _broker, _clock = _adapter(broker=Broker())
    with pytest.raises(IntegrationError) as error:
        getattr(adapter, operation)()
    assert error.value.code == "board_reachable"
    assert hub.calls == []


def test_ping_alone_keeps_the_board_reachable():
    adapter, hub, _broker, clock = _adapter(ping=0)
    _dark_for(adapter, clock, 600)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "board_reachable"
    assert hub.calls == []


def test_a_broker_outage_is_not_a_dark_board():
    adapter, hub, _broker, clock = _adapter(
        broker=Broker(ok=False, error_code="operation_unavailable")
    )
    _dark_for(adapter, clock, 600)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "board_reachable"
    assert hub.calls == []


@pytest.mark.parametrize("operation", ["jtag_check", "jtag_reset"])
def test_dark_for_too_short_is_refused(operation):
    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 200)
    with pytest.raises(IntegrationError) as error:
        getattr(adapter, operation)()
    assert error.value.code == "offline_not_established"
    assert hub.calls == []


def test_one_dark_probe_is_not_an_outage():
    adapter, hub, _broker, _clock = _adapter()
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "offline_not_established"
    assert hub.calls == []


def test_cached_observations_are_counted_once():
    adapter, hub, _broker, clock = _adapter()
    for _ in range(5):
        adapter.status()
    clock.now += 5  # inside the observer's refresh floor: the same probe
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "offline_not_established"
    assert hub.calls == []


def test_recent_kexec_restart_is_left_alone():
    adapter, hub, broker, clock = _adapter(broker=Broker())
    adapter.reboot()
    broker.ok, broker.error_code = False, "host_unreachable"
    clock.now += 60
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "restart_recent"
    assert hub.calls == []


# ----- the adapter: one request, then status ------------------------------------------


def test_check_after_a_dark_period_calls_the_agent_once():
    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    result = adapter.jtag_check()
    assert result["outcome"] == "check_passed"
    assert hub.calls == [(CHECK, None)]


def test_reset_after_a_dark_period_is_requested_not_confirmed():
    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    result = adapter.jtag_reset(idempotency_key="key")
    assert result["outcome"] == "reset_requested"
    assert result["reset_confirmed"] is False
    assert result["reset_issued_at"] == "2026-10-29T03:26:40.012Z"
    assert hub.calls == [(RESET, "key")]
    assert adapter.status()["last_jtag_reset_request"]["state"] == "requested"


def test_reset_is_confirmed_by_a_new_boot_of_the_same_board():
    adapter, _hub, broker, clock = _adapter(broker=Broker())
    adapter.status(refresh=True)  # remembers the healthy boot ID
    broker.ok, broker.error_code = False, "host_unreachable"
    clock.now += 60
    _dark_for(adapter, clock, 300)
    result = adapter.jtag_reset()
    assert result["previous_boot_id"] == "63bb230d-e96c-438c-917a-dcc464a80a44"

    broker.ok, broker.error_code, broker.output = True, None, NEW_BOOT
    clock.now += 80
    record = adapter.status(refresh=True)["last_jtag_reset_request"]
    assert record["state"] == "confirmed"
    assert record["boot_changed"] is True


def test_a_reset_the_board_never_answers_becomes_unconfirmed():
    adapter, _hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    clock.now += kr260_jtag.CONFIRM_SECONDS + 1
    record = adapter.status(refresh=True)["last_jtag_reset_request"]
    assert record["state"] == "unconfirmed"
    assert record["boot_changed"] is None


def test_second_reset_within_the_interval_is_refused():
    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    clock.now += 600
    adapter.status(refresh=True)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert len(hub.calls) == 1


@pytest.mark.parametrize(
    "result, counts",
    [
        # Refused by the agent before the wrapper ran.
        ({"success": False, "error": "board_reachable"}, False),
        ({"success": False, "error": "agent_unavailable"}, False),
        ({"success": False, "error": "debugger_active"}, False),
        (
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "REFUSED_CHAIN",
                "reset_may_have_run": False,
            },
            False,
        ),
        # Possibly sent.
        ({"success": False, "error": "timeout"}, True),
        ({"success": False, "error": "malformed_result"}, True),
        (
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "RST_ERROR",
                "reset_may_have_run": True,
            },
            True,
        ),
        (
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "NO_RESULT",
                "reset_may_have_run": True,
            },
            True,
        ),
    ],
)
def test_only_a_possibly_sent_reset_consumes_the_interval(result, counts):
    adapter, hub, _broker, clock = _adapter(hub=Hub(result))
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError):
        adapter.jtag_reset()
    assert ("last_jtag_reset_request" in adapter.status()) is counts
    hub.result = None
    if counts:
        with pytest.raises(IntegrationError) as error:
            adapter.jtag_reset()
        assert error.value.code == "reset_rate_limited"
    else:
        assert adapter.jtag_reset()["outcome"] == "reset_requested"


@pytest.mark.parametrize(
    "result, code",
    [
        (
            {"success": False, "error": "jtag_failed", "jtag_result": "REFUSED_CHAIN"},
            "jtag_refused_chain",
        ),
        (
            {"success": False, "error": "jtag_failed", "jtag_result": "CONNECT_FAILED"},
            "jtag_connect_failed",
        ),
        ({"success": False, "error": "debugger_active"}, "debugger_active"),
        ({"success": False}, "agent_action_failed"),
    ],
)
def test_failures_surface_as_fixed_codes(result, code):
    adapter, _hub, _broker, clock = _adapter(hub=Hub(result))
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_check()
    assert error.value.code == code


# ----- the hub: only fixed fields cross ---------------------------------------------------


def _hub(monkeypatch, reply):
    hub = AgentHub(AgentIngressSettings())
    sent = []

    def request(action, values, **kwargs):
        sent.append((action, values, kwargs))
        return {"action": action, **reply}

    monkeypatch.setattr(hub, "_request", request)
    return hub, sent


def test_hub_sends_no_parameters_and_projects_fixed_fields(monkeypatch):
    hub, sent = _hub(
        monkeypatch,
        {
            "success": True,
            "jtag_result": "RST_ISSUED",
            "reset_may_have_run": True,
            "reset_issued_at": "2026-10-29T03:26:40.012Z",
            "stdout": "anything",
            "stderr": "anything",
            "request_id": "r",
            "transport": "desktop_agent_wss",
        },
    )
    result = hub.kr260_jtag(RESET, idempotency_key="k")
    assert sent == [(RESET, {}, {"cancel": None, "idempotency_key": "k"})]
    assert result == {
        "request_id": "r",
        "transport": "desktop_agent_wss",
        "transport_state": "completed",
        "action": RESET,
        "success": True,
        "jtag_result": "RST_ISSUED",
        "reset_may_have_run": True,
        "reset_issued_at": "2026-10-29T03:26:40.012Z",
    }


@pytest.mark.parametrize(
    "action, reply",
    [
        (RESET, {"success": False, "jtag_result": "SOMETHING_NEW"}),
        (RESET, {"success": True, "jtag_result": "CHECK_OK"}),
        (CHECK, {"success": True, "jtag_result": "RST_ISSUED"}),
        (CHECK, {"success": True}),
    ],
)
def test_hub_rejects_unknown_or_mismatched_codes(monkeypatch, action, reply):
    hub, _sent = _hub(monkeypatch, reply)
    assert hub.kr260_jtag(action)["error"] == "malformed_result"


def test_hub_drops_a_malformed_reset_time_and_unknown_errors(monkeypatch):
    hub, _sent = _hub(
        monkeypatch,
        {
            "success": False,
            "error": "something the agent made up",
            "jtag_result": "RST_ERROR",
            "reset_may_have_run": "yes",
            "reset_issued_at": "03:26:40 yesterday",
        },
    )
    result = hub.kr260_jtag(RESET)
    assert result["error"] == "agent_action_failed"
    assert result["jtag_result"] == "RST_ERROR"
    assert "reset_may_have_run" not in result
    assert "reset_issued_at" not in result


def test_hub_refuses_any_other_action(monkeypatch):
    hub, sent = _hub(monkeypatch, {"success": True})
    assert hub.kr260_jtag("desktop.app.launch")["error"] == "invalid_action"
    assert sent == []


# ----- skills: administrator, fresh passkey, never conversational --------------------------


def test_jtag_skills_need_a_fresh_passkey_and_stay_out_of_the_planner(tmp_path):
    from test_nas_admin_controls import _application

    from butters.skills.model import AuthenticationLevel
    from butters.web.service import CONVERSATIONAL_PLANNER_ACTIONS

    _app, service, _nas = _application(tmp_path)
    for name in ("check_kr260_jtag", "reset_kr260_jtag"):
        spec = service.assistant.skills.get(name)
        assert spec.authentication is AuthenticationLevel.FRESH
        assert spec.local_console_allowed is False
        assert spec.confirmation_required is True
        # The shipped gate is closed.
        assert spec.available is False
        assert name not in CONVERSATIONAL_PLANNER_ACTIONS
