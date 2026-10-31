"""KR260 JTAG recovery through the Desktop Agent: Butters-side guards.

Nothing here reaches a cable: the agent hub is a fake, and the agent's own
refusals are tested in butters-agent/tests/test_kr260.py.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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
from butters.integrations.clock_trust import MAX_UNDETECTED_SECONDS
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
        self.state = "connected"
        self.offered = True

    def status(self):
        return {"state": self.state}

    def supports(self, action):
        return self.offered and action in {CHECK, RESET}

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
        observer,
        Actions(),
        reboot,
        jtag_settings=gate,
        monotonic=clock,
        clock_trusted=lambda: True,
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
            clock_trusted=True,
        )
        == expected
    )


def test_policy_constants_are_the_documented_ones():
    assert kr260_jtag.DARK_SECONDS == 300.0
    assert kr260_jtag.KEXEC_QUIET_SECONDS == 600.0
    assert kr260_jtag.POR_INTERVAL_SECONDS == 1800.0


VERIFIED = {"identity_verified": True, "operational_state": "online"}


@pytest.mark.parametrize(
    "observation, previous, age, expected",
    [
        ({**VERIFIED, "boot_id": "b"}, "a", 80.0, ("confirmed", "new_boot_id")),
        # XSDB said RST_ISSUED, but the same boot answers: nothing reset.
        ({**VERIFIED, "boot_id": "a"}, "a", 80.0, ("no_effect", "same_boot_id")),
        ({**VERIFIED, "boot_id": "a"}, "a", 400.0, ("no_effect", "same_boot_id")),
        # Previous boot unknown: never "confirmed", only "booted" by uptime.
        (
            {**VERIFIED, "uptime_seconds": 70.0},
            None,
            80.0,
            ("booted", "uptime_after_request"),
        ),
        (
            {**VERIFIED, "uptime_seconds": 9000.0},
            None,
            80.0,
            ("no_effect", "uptime_before_request"),
        ),
        ({**VERIFIED}, None, 80.0, ("requested", "identity_verified_without_boot_id")),
        # Never confirmed without a verified identity.
        (
            {
                "identity_verified": False,
                "operational_state": "identity_failed",
                "boot_id": "b",
            },
            "a",
            80.0,
            ("requested", "identity_mismatch"),
        ),
        (
            {"identity_verified": False, "operational_state": "identity_failed"},
            "a",
            400.0,
            ("unconfirmed", "identity_mismatch"),
        ),
        # Booting, or Linux without SSH: the network answers, Linux does not.
        (
            _offline(network_reachable=True, error="ssh_timeout"),
            "a",
            200.0,
            ("requested", "network_only"),
        ),
        (
            _offline(network_reachable=True, error="ssh_timeout"),
            "a",
            400.0,
            ("unconfirmed", "network_only"),
        ),
        (
            _offline(error="ssh_unavailable"),
            "a",
            80.0,
            ("requested", "monitoring_unavailable"),
        ),
        (_offline(), "a", 80.0, ("requested", "no_answer")),
        (_offline(), None, 400.0, ("unconfirmed", "no_answer")),
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
    assert adapter.status()["jtag_recovery"]["last_attempt"]["state"] == "check_passed"


def test_reset_after_a_dark_period_is_requested_not_confirmed():
    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    result = adapter.jtag_reset(idempotency_key="key")
    assert result["outcome"] == "reset_requested"
    assert result["reset_confirmed"] is False
    assert result["reset_issued_at"] == "2026-10-29T03:26:40.012Z"
    assert hub.calls == [(RESET, "key")]
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["state"] == "requested"
    assert attempt["jtag_result"] == "RST_ISSUED"


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
    record = adapter.status(refresh=True)["jtag_recovery"]["last_attempt"]
    assert record["state"] == "confirmed"
    assert record["evidence"] == "new_boot_id"
    assert record["boot_id"] == "329affab-9e29-4709-a3f0-f63080f12afa"


def test_a_reset_the_board_never_answers_becomes_unconfirmed():
    adapter, _hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    clock.now += kr260_jtag.CONFIRM_SECONDS + 1
    record = adapter.status(refresh=True)["jtag_recovery"]["last_attempt"]
    assert record["state"] == "unconfirmed"
    assert record["evidence"] == "no_answer"


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
        # The script's probe found the board answering before rst -por.
        (
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "REFUSED_REACHABLE",
                "reset_may_have_run": False,
            },
            False,
        ),
        (
            {"success": False, "error": "jtag_failed", "jtag_result": "REFUSED_PROBE"},
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
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["state"] == ("requested" if counts else "failed")
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
        (
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "REFUSED_REACHABLE",
            },
            "jtag_refused_reachable",
        ),
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


# ----- the Admin summary: Butters decides, the page only displays ------------------------


def _summary(adapter, refresh=True):
    return adapter.status(refresh=refresh)["jtag_recovery"]


def test_summary_for_a_healthy_board_is_online_and_blocked():
    adapter, _hub, _broker, _clock = _adapter(broker=Broker())
    summary = _summary(adapter)
    assert summary["board"] == "online"
    assert summary["reset_permitted"] is False
    assert summary["reset_blocked_reason"] == "board_reachable"
    assert summary["last_known_boot_id"] == "63bb230d-e96c-438c-917a-dcc464a80a44"
    assert summary["last_attempt"] is None


def test_summary_moves_from_unreachable_to_confirmed_unreachable():
    adapter, _hub, _broker, clock = _adapter()
    first = _summary(adapter)
    assert first["board"] == "unreachable"
    assert first["reset_blocked_reason"] == "offline_not_established"
    clock.now += 299
    assert _summary(adapter)["board"] == "unreachable"
    clock.now += 1
    summary = _summary(adapter)
    assert summary["board"] == "unreachable_confirmed"
    assert summary["dark_seconds"] == 300.0
    # The last read is one second after a probe: the cached one, not a third.
    assert summary["dark_probes"] == 2
    assert summary["reset_permitted"] is True
    assert summary["check_permitted"] is True


@pytest.mark.parametrize(
    "broker, ping, board",
    [
        (Broker(ok=False, error_code="host_unreachable"), 0, "degraded"),
        (Broker(ok=False, error_code="ssh_auth_failed"), 1, "degraded"),
        (Broker(ok=False, error_code="identity_mismatch"), 0, "degraded"),
        (
            Broker(ok=False, error_code="operation_unavailable"),
            1,
            "monitoring_unavailable",
        ),
    ],
)
def test_summary_never_calls_a_partial_answer_unreachable(broker, ping, board):
    adapter, _hub, _broker, clock = _adapter(broker=broker, ping=ping)
    _dark_for(adapter, clock, 600)
    summary = _summary(adapter)
    assert summary["board"] == board
    assert summary["reset_permitted"] is False


def test_summary_explains_each_blocked_layer():
    adapter, hub, _broker, clock = _adapter(enabled=False)
    _dark_for(adapter, clock, 600)
    assert _summary(adapter)["reset_blocked_reason"] == "gate_closed"

    adapter, hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 600)
    hub.state = "disconnected"
    assert _summary(adapter)["reset_blocked_reason"] == "agent_disconnected"
    hub.state, hub.offered = "connected", False
    # The agent connected without its local gate: it offers no JTAG action.
    assert _summary(adapter)["reset_blocked_reason"] == "agent_jtag_not_offered"
    hub.offered = True
    assert _summary(adapter)["reset_permitted"] is True

    adapter.attach_jtag(None)
    assert _summary(adapter)["reset_blocked_reason"] == "agent_unconfigured"


def test_summary_reports_the_kexec_quiet_period():
    adapter, _hub, broker, clock = _adapter(broker=Broker())
    adapter.reboot()
    broker.ok, broker.error_code = False, "host_unreachable"
    _dark_for(adapter, clock, 300)
    assert _summary(adapter)["reset_blocked_reason"] == "restart_recent"
    clock.now += kr260_jtag.KEXEC_QUIET_SECONDS
    assert _summary(adapter)["reset_permitted"] is True


def test_summary_after_a_reset_is_rate_limited_but_check_is_not():
    adapter, _hub, _broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    summary = _summary(adapter)
    assert summary["reset_blocked_reason"] == "reset_rate_limited"
    assert summary["check_permitted"] is True


# ----- races between authorization and execution -------------------------------------------


def test_board_recovering_before_execution_is_refused():
    adapter, hub, broker, clock = _adapter()
    _dark_for(adapter, clock, 300)
    assert _summary(adapter, refresh=False)["reset_permitted"] is True
    # The passkey ceremony takes a while; the board comes back meanwhile.
    broker.ok, broker.error_code = True, None
    clock.now += 15
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "board_reachable"
    assert hub.calls == []
    assert _summary(adapter)["last_attempt"] is None


def test_concurrent_requests_send_one_reset():
    import threading

    started, release = threading.Event(), threading.Event()

    class SlowHub(Hub):
        def kr260_jtag(self, action, **kwargs):
            started.set()
            release.wait(5)
            return super().kr260_jtag(action, **kwargs)

    adapter, hub, _broker, clock = _adapter(hub=SlowHub())
    _dark_for(adapter, clock, 300)
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault("first", adapter.jtag_reset())
    )
    worker.start()
    assert started.wait(5)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "busy"
    release.set()
    worker.join(5)
    assert outcome["first"]["outcome"] == "reset_requested"
    assert len(hub.calls) == 1
    # And afterwards the interval refuses a second one.
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"


# ----- disconnects, XSDB errors and missing evidence ------------------------------------------


@pytest.mark.parametrize("error", ["agent_disconnected", "transport_error", "timeout"])
def test_agent_lost_mid_request_counts_as_possibly_reset(error):
    adapter, _hub, _broker, clock = _adapter(
        hub=Hub({"success": False, "error": error})
    )
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as raised:
        adapter.jtag_reset()
    assert raised.value.code == error
    attempt = _summary(adapter)["last_attempt"]
    assert attempt["state"] == "requested"
    assert attempt["error"] == error
    assert _summary(adapter)["reset_blocked_reason"] == "reset_rate_limited"


def test_a_hub_exception_counts_as_possibly_reset():
    class BrokenHub(Hub):
        def kr260_jtag(self, action, **kwargs):
            raise RuntimeError("socket gone")

    adapter, _hub, _broker, clock = _adapter(hub=BrokenHub())
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as raised:
        adapter.jtag_reset()
    assert raised.value.code == "transport_error"
    assert _summary(adapter)["reset_blocked_reason"] == "reset_rate_limited"


def test_xsdb_error_is_a_failure_that_may_still_have_reset():
    adapter, _hub, _broker, clock = _adapter(
        hub=Hub(
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "RST_ERROR",
                "reset_may_have_run": True,
            }
        )
    )
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as raised:
        adapter.jtag_reset()
    assert raised.value.code == "jtag_rst_error"
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["error"]) == ("requested", "jtag_rst_error")


def test_wrong_cable_or_chain_is_a_failure_that_sent_nothing():
    adapter, _hub, _broker, clock = _adapter(
        hub=Hub(
            {
                "success": False,
                "error": "jtag_failed",
                "jtag_result": "REFUSED_CHAIN",
                "reset_may_have_run": False,
            }
        )
    )
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as raised:
        adapter.jtag_reset()
    assert raised.value.code == "jtag_refused_chain"
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("failed", "not_sent")
    assert _summary(adapter)["reset_permitted"] is True


def _after_reset(broker_after, *, ping_after=1, seconds=80):
    adapter, _hub, broker, clock = _adapter(broker=Broker())
    adapter.status(refresh=True)
    broker.ok, broker.error_code = False, "host_unreachable"
    clock.now += 60
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    broker_after(broker)
    adapter.observer.runner.ping = ping_after
    clock.now += seconds
    return adapter, clock, broker


def test_rst_issued_but_the_same_boot_answers_is_no_effect():
    adapter, _clock, _broker = _after_reset(
        lambda b: (setattr(b, "ok", True), setattr(b, "error_code", None)), ping_after=0
    )
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("no_effect", "same_boot_id")
    assert attempt["jtag_result"] == "RST_ISSUED"


def test_boot_that_never_reaches_linux_ends_unconfirmed():
    # The network answers (booting) but SSH never does.
    adapter, clock, _broker = _after_reset(
        lambda b: setattr(b, "error_code", "operation_timeout"), ping_after=0
    )
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("requested", "network_only")
    clock.now += kr260_jtag.CONFIRM_SECONDS
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("unconfirmed", "network_only")


def test_a_different_machine_answering_is_never_confirmed():
    other = NEW_BOOT.replace(
        "machine_id=7a6ccc6abe67407b93558d1efcb36587", "machine_id=" + "0" * 32
    )

    def swap(broker):
        broker.ok, broker.error_code, broker.output = True, None, other

    adapter, clock, _broker = _after_reset(swap, ping_after=0)
    attempt = _summary(adapter)["last_attempt"]
    assert attempt["state"] == "requested"
    assert attempt["evidence"] == "identity_mismatch"
    assert attempt["boot_id"] is None
    clock.now += kr260_jtag.CONFIRM_SECONDS
    assert _summary(adapter)["last_attempt"]["state"] == "unconfirmed"


def test_failed_monitoring_after_a_reset_is_not_evidence():
    adapter, clock, _broker = _after_reset(
        lambda b: setattr(b, "error_code", "operation_unavailable")
    )
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == (
        "requested",
        "monitoring_unavailable",
    )
    clock.now += kr260_jtag.CONFIRM_SECONDS
    assert _summary(adapter)["last_attempt"]["state"] == "unconfirmed"


def test_confirmation_is_kept_if_the_board_goes_dark_again():
    def recover(broker):
        broker.ok, broker.error_code, broker.output = True, None, NEW_BOOT

    adapter, clock, broker = _after_reset(recover, ping_after=0)
    assert _summary(adapter)["last_attempt"]["state"] == "confirmed"
    broker.ok, broker.error_code = False, "host_unreachable"
    adapter.observer.runner.ping = 1
    clock.now += 60
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("confirmed", "new_boot_id")


def test_a_slow_boot_can_still_be_confirmed_after_unconfirmed():
    adapter, clock, broker = _after_reset(
        lambda b: None, seconds=kr260_jtag.CONFIRM_SECONDS + 10
    )
    assert _summary(adapter)["last_attempt"]["state"] == "unconfirmed"
    broker.ok, broker.error_code, broker.output = True, None, NEW_BOOT
    adapter.observer.runner.ping = 0
    clock.now += 30
    assert _summary(adapter)["last_attempt"]["state"] == "confirmed"


# ----- the interval survives a Butters restart ------------------------------------------------


def test_a_persisted_reset_job_rate_limits_a_fresh_process():
    adapter, hub, _broker, clock = _adapter()
    wall = {"now": 50_000.0}
    adapter.clock = lambda: wall["now"]
    adapter.attach_jtag(hub, persisted_reset_at=lambda: wall["now"] - 600)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []
    # Persisted times count MAX_UNDETECTED_SECONDS later (clock_trust.py).
    adapter.attach_jtag(
        hub, persisted_reset_at=lambda: wall["now"] - 1801 - MAX_UNDETECTED_SECONDS
    )
    assert adapter.jtag_reset()["outcome"] == "reset_requested"


def test_a_persisted_kexec_job_keeps_the_quiet_period_in_a_fresh_process():
    # A kexec restart requested before a Butters restart: the new process has
    # no in-memory record of it, but the job store does.
    adapter, hub, _broker, clock = _adapter()
    wall = {"now": 50_000.0}
    adapter.clock = lambda: wall["now"]
    adapter.attach_jtag(hub, persisted_kexec_at=lambda: wall["now"] - 360)
    _dark_for(adapter, clock, 300)
    summary = adapter.status()["jtag_recovery"]
    assert summary["reset_blocked_reason"] == "restart_recent"
    assert summary["check_blocked_reason"] == "restart_recent"
    for run in (adapter.jtag_check, adapter.jtag_reset):
        with pytest.raises(IntegrationError) as error:
            run()
        assert error.value.code == "restart_recent"
    assert hub.calls == []
    adapter.attach_jtag(
        hub,
        persisted_kexec_at=lambda: (
            wall["now"] - kr260_jtag.KEXEC_QUIET_SECONDS - MAX_UNDETECTED_SECONDS - 1
        ),
    )
    assert adapter.jtag_reset()["outcome"] == "reset_requested"


def test_the_later_of_the_in_memory_and_persisted_kexec_times_counts():
    adapter, hub, broker, clock = _adapter(broker=Broker())
    wall = {"now": 50_000.0}
    adapter.clock = lambda: wall["now"]
    adapter.reboot()
    broker.ok, broker.error_code = False, "host_unreachable"
    clock.now += kr260_jtag.KEXEC_QUIET_SECONDS + 1
    wall["now"] += kr260_jtag.KEXEC_QUIET_SECONDS + 1
    # The in-memory restart is old enough; a newer stored one is not.
    adapter.attach_jtag(hub, persisted_kexec_at=lambda: wall["now"] - 200)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "restart_recent"
    assert hub.calls == []


def _raises():
    import sqlite3

    raise sqlite3.OperationalError("database is locked")


def test_an_unreadable_job_store_refuses_without_breaking_status():
    adapter, hub, _broker, clock = _adapter()
    adapter.attach_jtag(hub, persisted_kexec_at=_raises, persisted_reset_at=_raises)
    _dark_for(adapter, clock, 300)
    # Monitoring keeps working; both operations are refused as if a restart
    # and a reset had just happened.
    status = adapter.status()
    assert status["jtag_recovery"]["board"] == "unreachable_confirmed"
    assert status["jtag_recovery"]["reset_blocked_reason"] == "restart_recent"
    assert status["jtag_recovery"]["check_blocked_reason"] == "restart_recent"
    for run in (adapter.jtag_check, adapter.jtag_reset):
        with pytest.raises(IntegrationError) as error:
            run()
        assert error.value.code == "restart_recent"
    assert hub.calls == []


def test_an_unreadable_reset_history_keeps_the_reset_interval():
    adapter, hub, _broker, clock = _adapter()
    adapter.attach_jtag(hub, persisted_reset_at=_raises)
    _dark_for(adapter, clock, 300)
    summary = adapter.status()["jtag_recovery"]
    assert summary["reset_blocked_reason"] == "reset_rate_limited"
    # A check never resets, so the reset interval does not block it.
    assert summary["check_permitted"] is True
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []


@pytest.mark.parametrize(
    "stored", ["1000", True, float("nan"), float("-inf"), float("inf"), object()]
)
@pytest.mark.parametrize(
    "which, code",
    [
        ("persisted_kexec_at", "restart_recent"),
        ("persisted_reset_at", "reset_rate_limited"),
    ],
)
def test_a_malformed_stored_time_is_never_trusted_to_be_old(stored, which, code):
    adapter, hub, _broker, clock = _adapter()
    adapter.attach_jtag(hub, **{which: lambda: stored})
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == code
    assert hub.calls == []


@pytest.mark.parametrize(
    "which, code",
    [
        ("persisted_kexec_at", "restart_recent"),
        ("persisted_reset_at", "reset_rate_limited"),
    ],
)
def test_a_stored_time_ahead_of_the_clock_never_shortens_a_wait(which, code):
    # The wall clock stepped back after the job was stored: the job counts as
    # just now, not as negative age.
    adapter, hub, _broker, clock = _adapter()
    wall = {"now": 50_000.0}
    adapter.clock = lambda: wall["now"]
    adapter.attach_jtag(hub, **{which: lambda: wall["now"] + 86_400})
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == code
    assert hub.calls == []


def test_a_kexec_job_survives_a_store_reopen_and_an_interrupted_restart(tmp_path):
    from butters.actions.store import ActionStateStore

    path = tmp_path / "actions.sqlite3"
    actions = load_assistant_settings().actions
    lookup_args = ("reboot_kr260",)
    first = ActionStateStore(path, actions, clock=lambda: 1000.0)
    with first._lock, first._connect() as connection:
        connection.execute(
            "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
            "created_at,state,stage) VALUES (?,?,?,?,?,?,?,?,?)",
            ("j1", "p", "reboot_kr260", "s", "s", "i", 1000.0, "running", "running"),
        )
    # Still running in that process: not counted (the caller is usually one).
    assert (
        first.last_job_that_may_have_run(
            *lookup_args, not_run=Kr260Adapter.RESTART_NOT_SENT
        )
        is None
    )
    # Butters restarts: the web service fails interrupted jobs at startup, and
    # an interrupted restart may have run.
    second = ActionStateStore(path, actions, clock=lambda: 1300.0)
    assert second.recover_interrupted_jobs(local_console=False) == 1
    third = ActionStateStore(path, actions, clock=lambda: 1400.0)
    assert (
        third.last_job_that_may_have_run(
            *lookup_args, not_run=Kr260Adapter.RESTART_NOT_SENT
        )
        == 1000.0
    )


def test_store_finds_only_kexec_jobs_that_may_have_run(tmp_path):
    from butters.actions.store import ActionStateStore

    times = iter(range(1000, 2000, 10))
    store = ActionStateStore(
        tmp_path / "actions.sqlite3",
        load_assistant_settings().actions,
        clock=lambda: float(next(times)),
    )

    def job(skill, state, failure=None):
        with store._lock, store._connect() as connection:
            stamp = store.clock()
            connection.execute(
                "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
                "created_at,state,stage,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"j{stamp}", "p", skill, "s", "s", "i", stamp, state, state, failure),
            )
            return stamp

    lookup = lambda: store.last_job_that_may_have_run(
        "reboot_kr260", not_run=Kr260Adapter.RESTART_NOT_SENT
    )
    assert lookup() is None
    # Refused before anything reached the board: no quiet period.
    for code in (
        "identity_unverified",
        "reboot_in_progress",
        "host_unreachable",
        "restart_already_staged",
        "capability_unavailable",
    ):
        job("reboot_kr260", "failed", code)
    job("reset_kr260_jtag", "completed")
    assert lookup() is None
    # Unknown outcomes count: a timeout, or a job interrupted by a restart.
    timed_out = job("reboot_kr260", "failed", "timeout")
    assert lookup() == timed_out
    interrupted = job("reboot_kr260", "failed", "restart_recovery_required")
    assert lookup() == interrupted
    completed = job("reboot_kr260", "completed")
    assert lookup() == completed


def test_restart_not_sent_extends_the_broker_refusals_with_the_adapter_checks():
    assert Kr260Adapter._NOT_SENT < Kr260Adapter.RESTART_NOT_SENT
    assert Kr260Adapter.RESTART_NOT_SENT - Kr260Adapter._NOT_SENT == {
        "capability_unavailable",
        "identity_unverified",
        "reboot_in_progress",
        "clock_unsynchronized",
        "safety_state_unavailable",
    }
    # Nothing that may have restarted the board is treated as not sent.
    for code in (
        "timeout",
        "restart_recovery_required",
        "internal_error",
        "malformed_result",
    ):
        assert code not in Kr260Adapter.RESTART_NOT_SENT


def test_store_finds_only_reset_jobs_that_may_have_run(tmp_path):
    from butters.actions.store import ActionStateStore

    times = iter(range(1000, 2000, 10))
    store = ActionStateStore(
        tmp_path / "actions.sqlite3",
        load_assistant_settings().actions,
        clock=lambda: float(next(times)),
    )

    def job(skill, state, failure=None):
        with store._lock, store._connect() as connection:
            connection.execute(
                "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
                "created_at,state,stage,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"j{store.clock()}",
                    "p",
                    skill,
                    "s",
                    "s",
                    "i",
                    store.clock(),
                    state,
                    state,
                    failure,
                ),
            )

    lookup = lambda: store.last_job_that_may_have_run(
        "reset_kr260_jtag", not_run=kr260_jtag.RESET_NOT_SENT
    )
    assert lookup() is None
    job("reset_kr260_jtag", "failed", "board_reachable")
    job("reset_kr260_jtag", "failed", "jtag_refused_chain")
    job("reboot_kr260", "completed")
    assert lookup() is None
    job("reset_kr260_jtag", "failed", "restart_recovery_required")
    interrupted = lookup()
    assert interrupted is not None
    job("reset_kr260_jtag", "completed")
    completed = lookup()
    assert completed is not None and completed > interrupted
    # The running job itself (the caller) is never counted.
    job("reset_kr260_jtag", "running")
    assert lookup() == completed


def test_hub_supports_only_what_the_current_connection_advertised():
    hub = AgentHub(AgentIngressSettings())
    assert hub.supports(RESET) is False
    hub.connection_id, hub._actions = "c", ("desktop.app.list", RESET)
    assert hub.supports(RESET) is True
    assert hub.supports(CHECK) is False
    hub.connection_id = None
    assert hub.supports(RESET) is False


def test_hub_projects_the_cleanup_report(monkeypatch):
    hub, _sent = _hub(
        monkeypatch,
        {"success": True, "jtag_result": "CHECK_OK", "debugger_cleanup": "forced"},
    )
    assert hub.kr260_jtag(CHECK)["debugger_cleanup"] == "forced"
    hub, _sent = _hub(
        monkeypatch,
        {"success": True, "jtag_result": "CHECK_OK", "debugger_cleanup": "rm -rf"},
    )
    assert "debugger_cleanup" not in hub.kr260_jtag(CHECK)


# ----- failures never escalate -----------------------------------------------------------


def test_jtag_paths_reach_no_other_power_action():
    import inspect

    from butters.integrations import actions

    source = "".join(
        inspect.getsource(getattr(actions.Kr260Adapter, name))
        for name in ("jtag_check", "jtag_reset", "_jtag_run", "_may_have_reset")
    ) + inspect.getsource(kr260_jtag)
    for word in (
        "BrokerOperation",
        "self.actions",
        "desktop.",
        "wake",
        "shutdown",
        "monitors_",
        "reboot(",
        "kexec_restart",
    ):
        assert word not in source, word
    agent = (
        Path(__file__).parents[2] / "butters-agent/src/butters_agent/kr260.py"
    ).read_text()
    for word in (
        "shutdown",
        "Restart-Computer",
        "Stop-Computer",
        "wake",
        "monitors",
        "rst -system",
    ):
        assert word not in agent, word


def test_a_failed_reset_never_triggers_a_kexec_or_another_call():
    adapter, hub, _broker, clock = _adapter(
        hub=Hub({"success": False, "error": "timeout"})
    )
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError):
        adapter.jtag_reset()
    assert adapter.actions.calls == []
    assert len(hub.calls) == 1


def test_the_request_is_timed_from_before_it_was_sent():
    """A reset happens during the ~30 s request; uptime counts from then."""

    class SlowHub(Hub):
        def kr260_jtag(self, action, **kwargs):
            clock.now += 25  # the agent's run
            return super().kr260_jtag(action, **kwargs)

    adapter, _hub, broker, clock = _adapter(hub=SlowHub())
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    # Booted 15 s into the request; observed 60 s after it returned.
    broker.ok, broker.error_code = True, None
    broker.output = NEW_BOOT.replace("uptime=80.00", "uptime=70.00")
    adapter.observer.runner.ping = 0
    clock.now += 60
    attempt = _summary(adapter)["last_attempt"]
    assert (attempt["state"], attempt["evidence"]) == ("booted", "uptime_after_request")


def test_butters_and_the_agent_agree_on_every_wrapper_result_code():
    from butters_agent import kr260 as agent_kr260

    assert agent_kr260.RESULT_CODES == AgentHub._KR260_JTAG_RESULTS
    # Every code after which the agent says no POR can have run is one Butters
    # also treats as not sent, so a restarted Butters keeps the same interval.
    for code in agent_kr260.RESULT_CODES - agent_kr260._MAY_HAVE_RESET:
        if code in agent_kr260.SUCCESS.values():
            continue
        assert f"jtag_{code.lower()}" in kr260_jtag.RESET_NOT_SENT, code
    for code in agent_kr260._MAY_HAVE_RESET:
        assert f"jtag_{code.lower()}" not in kr260_jtag.RESET_NOT_SENT, code


def test_the_scripts_refusal_is_not_a_reset_and_needs_a_new_request():
    hub = Hub(
        {
            "success": False,
            "error": "jtag_failed",
            "jtag_result": "REFUSED_REACHABLE",
            "reset_may_have_run": False,
        }
    )
    adapter, hub, _broker, clock = _adapter(hub=hub)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "jtag_refused_reachable"
    assert len(hub.calls) == 1
    summary = adapter.status()["jtag_recovery"]
    assert summary["last_attempt"]["state"] == "failed"
    assert summary["reset_blocked_reason"] != "reset_rate_limited"
