from __future__ import annotations

import hashlib
import subprocess

import pytest
from butters_agent import kr260
from butters_agent.__main__ import load_kr260
from butters_agent.engine import Engine
from butters_agent.platform.fake import Platform
from butters_agent.protocol import OPTIONAL, SCHEMAS, ProtocolError

WRAPPER_BODY = b"# wrapper\n"
SCRIPT_BODY = b"# tcl\n"
CHECK_OK = (
    "2026-10-29T03:01:58.000Z xsdb start mode=check cable=XFL11R5N0BBZA\n"
    "2026-10-29T03:02:12.343Z RESULT CHECK_OK cable=XFL11R5N0BBZA\n"
)
RST_ISSUED = (
    "2026-10-29T03:26:30.000Z xsdb start mode=por cable=XFL11R5N0BBZA\n"
    "2026-10-29T03:26:42.000Z RESULT RST_ISSUED at=2026-10-29T03:26:40.012Z"
    " cable=XFL11R5N0BBZA\n"
)


def config(**overrides):
    value = {
        "schema_version": 1,
        "enabled": True,
        "cable_serial": "XFL11R5N0BBZA",
        "board_address": "192.168.1.125",
        "wrapper_sha256": hashlib.sha256(WRAPPER_BODY).hexdigest(),
        "script_sha256": hashlib.sha256(SCRIPT_BODY).hexdigest(),
    }
    value.update(overrides)
    return value


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def platform():
    fake = Platform()
    fake.files = {str(kr260.WRAPPER): WRAPPER_BODY, str(kr260.SCRIPT): SCRIPT_BODY}
    return fake


@pytest.fixture
def clock():
    return Clock()


def engine_for(platform, clock, **overrides):
    return Engine(platform, kr260=kr260.Kr260Jtag(platform, config(**overrides), clock))


# ----- advertisement: a default hello is unchanged ----------------------------


def test_default_engine_advertises_no_optional_action():
    actions = Engine(Platform()).actions()
    assert actions == sorted(set(SCHEMAS) - OPTIONAL)
    assert not set(actions) & OPTIONAL


def test_disabled_config_advertises_nothing_and_refuses(platform, clock):
    engine = engine_for(platform, clock, enabled=False)
    assert not set(engine.actions()) & OPTIONAL
    result = engine.invoke(kr260.RESET, {})
    assert result["success"] is False
    assert result["error"] == "unsupported_action"
    assert platform.wrapper_calls == []


def test_enabled_config_advertises_both_actions(platform, clock):
    assert set(engine_for(platform, clock).actions()) >= kr260.ACTIONS


def test_engine_without_kr260_refuses_optional_actions():
    result = Engine(Platform()).invoke(kr260.CHECK, {})
    assert result["error"] == "unsupported_action"


def test_parameters_are_refused(platform, clock):
    with pytest.raises(ProtocolError, match="invalid_parameter"):
        engine_for(platform, clock).invoke(kr260.RESET, {"cable": "x"})
    assert platform.wrapper_calls == []


# ----- fixed argv ---------------------------------------------------------------


def test_check_runs_fixed_wrapper_arguments(platform, clock):
    platform.wrapper_outcome = (0, CHECK_OK)
    result = engine_for(platform, clock).invoke(kr260.CHECK, {})
    assert result["success"] is True
    assert result["jtag_result"] == "CHECK_OK"
    assert "reset_may_have_run" not in result
    assert platform.wrapper_calls == [
        (
            str(kr260.WRAPPER),
            [
                "-Mode",
                "check",
                "-CableSerial",
                "XFL11R5N0BBZA",
                "-TimeoutSeconds",
                str(kr260.WRAPPER_TIMEOUT_SECONDS),
            ],
            kr260.RUN_TIMEOUT_SECONDS,
        )
    ]


def test_reset_runs_fixed_wrapper_arguments_with_confirmation(platform, clock):
    platform.wrapper_outcome = (0, RST_ISSUED)
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert result["success"] is True
    assert result["jtag_result"] == "RST_ISSUED"
    assert result["reset_issued_at"] == "2026-10-29T03:26:40.012Z"
    assert result["reset_may_have_run"] is True
    ((_, arguments, _),) = platform.wrapper_calls
    assert arguments[:2] == ["-Mode", "por"]
    assert arguments[-2:] == ["-Confirm", "confirm-por"]


def test_run_timeout_budget_fits_the_request_window():
    assert kr260.WRAPPER_TIMEOUT_SECONDS < kr260.RUN_TIMEOUT_SECONDS
    assert (
        kr260.PREFLIGHT_SECONDS + kr260.RUN_TIMEOUT_SECONDS + kr260.CLEANUP_SECONDS
        <= 30
    )
    # The supervised attach-only check took 14.6 s end to end.
    assert kr260.WRAPPER_TIMEOUT_SECONDS > 14.6


# ----- refusals: none starts the wrapper ------------------------------------------


@pytest.mark.parametrize(
    "arrange, error",
    [
        (lambda p: setattr(p, "board_pings", True), "board_reachable"),
        (lambda p: p.debuggers.add("hw_server.exe"), "debugger_active"),
        (lambda p: p.files.pop(str(kr260.SCRIPT)), "integrity_failed"),
        (lambda p: p.files.update({str(kr260.WRAPPER): b"edited"}), "integrity_failed"),
        (lambda p: p.unprotected.add(str(kr260.WRAPPER)), "integrity_failed"),
    ],
)
@pytest.mark.parametrize("action", sorted(kr260.ACTIONS))
def test_refusals_never_start_the_wrapper(platform, clock, arrange, error, action):
    arrange(platform)
    result = engine_for(platform, clock).invoke(action, {})
    assert result["success"] is False
    assert result["error"] == error
    assert platform.wrapper_calls == []


def test_refused_reset_does_not_consume_the_interval(platform, clock):
    engine = engine_for(platform, clock)
    platform.board_pings = True
    assert engine.invoke(kr260.RESET, {})["error"] == "board_reachable"
    platform.board_pings = False
    platform.wrapper_outcome = (0, RST_ISSUED)
    assert engine.invoke(kr260.RESET, {})["success"] is True


# ----- one reset attempt per interval -------------------------------------------------


def test_second_reset_within_interval_is_rate_limited(platform, clock):
    engine = engine_for(platform, clock)
    platform.wrapper_outcome = (0, RST_ISSUED)
    assert engine.invoke(kr260.RESET, {})["success"] is True
    clock.now += kr260.RESET_INTERVAL_SECONDS - 1
    assert engine.invoke(kr260.RESET, {})["error"] == "rate_limited"
    assert len(platform.wrapper_calls) == 1
    clock.now += 2
    assert engine.invoke(kr260.RESET, {})["success"] is True


def test_failed_reset_attempt_still_consumes_the_interval(platform, clock):
    engine = engine_for(platform, clock)
    platform.wrapper_outcome = subprocess.TimeoutExpired("powershell", 27)
    result = engine.invoke(kr260.RESET, {})
    assert result["jtag_result"] == "NO_RESULT"
    assert result["reset_may_have_run"] is True
    assert engine.invoke(kr260.RESET, {})["error"] == "rate_limited"


def test_checks_are_not_rate_limited_by_a_reset(platform, clock):
    engine = engine_for(platform, clock)
    platform.wrapper_outcome = (0, RST_ISSUED)
    engine.invoke(kr260.RESET, {})
    platform.wrapper_outcome = (0, CHECK_OK)
    assert engine.invoke(kr260.CHECK, {})["success"] is True


def test_busy_when_a_run_is_in_progress(platform, clock):
    jtag = kr260.Kr260Jtag(platform, config(), clock)
    jtag.lock.acquire()
    result = Engine(platform, kr260=jtag).invoke(kr260.CHECK, {})
    assert result["error"] == "busy"


# ----- result parsing: fixed codes only ------------------------------------------------


@pytest.mark.parametrize(
    "returncode, output, code, may_have_run",
    [
        (
            1,
            "2026-10-29T03:00:00Z RESULT REFUSED_CHAIN chain 'x' is not 'y'\n",
            "REFUSED_CHAIN",
            False,
        ),
        (
            1,
            "2026-10-29T03:00:00Z RESULT REFUSED_DEBUGGER_ACTIVE hw_server:1\n",
            "REFUSED_DEBUGGER_ACTIVE",
            False,
        ),
        (
            1,
            "2026-10-29T03:00:00Z RESULT CONNECT_FAILED no hw_server\n",
            "CONNECT_FAILED",
            False,
        ),
        (
            1,
            "2026-10-29T03:00:00Z RESULT RST_ERROR at=2026-10-29T03:00:00.000Z boom\n",
            "RST_ERROR",
            True,
        ),
        (1, "2026-10-29T03:00:00Z RESULT SOMETHING_NEW\n", "NO_RESULT", True),
        (1, "RESULT RST_ISSUED at=2026-10-29T03:00:00.000Z\n", "NO_RESULT", True),
        (1, "", "NO_RESULT", True),
        # A success code with a failing wrapper exit is not a success.
        (1, RST_ISSUED, "RST_ISSUED", True),
    ],
)
def test_reset_results_are_fixed_codes(
    platform, clock, returncode, output, code, may_have_run
):
    platform.wrapper_outcome = (returncode, output)
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert result["success"] is False
    assert result["jtag_result"] == code
    assert result["reset_may_have_run"] is may_have_run
    assert result["error"] == "jtag_failed"
    if code != "RST_ISSUED":
        assert result["reset_issued_at"] is None
    # No wrapper text crosses the wire.
    assert set(result) <= {
        "action",
        "target_host",
        "started_at",
        "success",
        "exit_code",
        "stdout",
        "stderr",
        "jtag_result",
        "error",
        "reset_may_have_run",
        "reset_issued_at",
        "completed_at",
        "duration_seconds",
        "debugger_cleanup",
    }
    assert result["stdout"] == result["stderr"] == ""


def test_last_result_line_wins():
    output = (
        "2026-10-29T03:00:00Z RESULT CHECK_OK cable=XFL11R5N0BBZA\n"
        "2026-10-29T03:00:01Z RESULT RST_ERROR at=2026-10-29T03:00:01.000Z x\n"
    )
    assert kr260.parse_result(output) == ("RST_ERROR", None)


# ----- local configuration -------------------------------------------------------------


def test_config_accepts_the_documented_shape():
    assert kr260.load_config(_toml(config()))["cable_serial"] == "XFL11R5N0BBZA"


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"enabled": "true"},
        {"cable_serial": "XFL11R5N0BBZ"},
        {"cable_serial": "xfl11r5n0bbza"},
        {"board_address": "8.8.8.8"},
        {"board_address": "kria.local"},
        {"board_address": "192.168.001.125"},
        {"wrapper_sha256": "0" * 63},
        {"script_sha256": "A" * 64},
        {"extra": "x"},
    ],
)
def test_config_rejects_anything_else(overrides):
    with pytest.raises(ValueError):
        kr260.load_config(_toml(config(**overrides)))


def test_missing_config_file_leaves_the_feature_absent(tmp_path):
    assert load_kr260(Platform(), tmp_path / "config.toml") is None


def test_invalid_config_file_disables_only_the_feature(tmp_path):
    path = tmp_path / "kr260-jtag.toml"
    path.write_text("schema_version = 1\nenabled = true\n", encoding="utf-8")
    path.chmod(0o600)
    assert load_kr260(Platform(), tmp_path / "config.toml") is None


def test_writable_config_file_is_refused(tmp_path):
    path = tmp_path / "kr260-jtag.toml"
    path.write_text(_toml(config()), encoding="utf-8")
    path.chmod(0o666)
    assert load_kr260(Platform(), tmp_path / "config.toml") is None


def test_valid_config_file_loads(tmp_path):
    path = tmp_path / "kr260-jtag.toml"
    path.write_text(_toml(config()), encoding="utf-8")
    path.chmod(0o600)
    loaded = load_kr260(Platform(), tmp_path / "config.toml")
    assert loaded is not None and loaded.actions() == kr260.ACTIONS


def _toml(values):
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f'{key} = "{value}"')
    return "\n".join(lines) + "\n"


# ----- reachability is rechecked immediately before the wrapper -------------------


@pytest.mark.parametrize("action", sorted(kr260.ACTIONS))
def test_board_returning_during_the_checks_stops_the_run(platform, clock, action):
    # Dark at the first ping, answering at the recheck just before the wrapper.
    platform.ping_sequence = [False, True]
    result = engine_for(platform, clock).invoke(action, {})
    assert result["error"] == "board_reachable"
    assert platform.wrapper_calls == []
    assert platform.ping_calls == 2


def test_both_pings_run_and_the_last_precedes_the_wrapper(platform, clock):
    order = []
    ping = platform.host_answers_ping
    run = platform.run_wrapper
    platform.host_answers_ping = lambda address: order.append("ping") or ping(address)
    platform.run_wrapper = lambda *a: order.append("run") or run(*a)
    platform.wrapper_outcome = (0, RST_ISSUED)
    engine_for(platform, clock).invoke(kr260.RESET, {})
    assert order == ["ping", "ping", "run"]


def test_a_reset_refused_at_the_recheck_does_not_consume_the_interval(platform, clock):
    engine = engine_for(platform, clock)
    platform.ping_sequence = [False, True]
    assert engine.invoke(kr260.RESET, {})["error"] == "board_reachable"
    platform.wrapper_outcome = (0, RST_ISSUED)
    assert engine.invoke(kr260.RESET, {})["success"] is True


# ----- debugger cleanup ------------------------------------------------------------


def test_clean_run_reports_clean(platform, clock):
    platform.wrapper_outcome = (0, CHECK_OK)
    result = engine_for(platform, clock).invoke(kr260.CHECK, {})
    assert result["debugger_cleanup"] == "clean"
    assert platform.stop_calls == 0


def test_leftover_debuggers_are_stopped(platform, clock):
    platform.wrapper_outcome = (0, RST_ISSUED)
    platform.debuggers_after_run = {"hw_server.exe"}
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert result["debugger_cleanup"] == "forced"
    assert platform.stop_calls == 1
    assert platform.debuggers == set()
    # A forced cleanup does not turn a reset into a failure or a retry.
    assert result["success"] is True
    assert len(platform.wrapper_calls) == 1


def test_debuggers_that_survive_cleanup_are_reported(platform, clock):
    platform.wrapper_outcome = subprocess.TimeoutExpired("powershell", 20)
    platform.debuggers_after_run = {"xsdb.exe"}
    platform.debuggers_survive = True
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert result["debugger_cleanup"] == "failed"
    assert result["jtag_result"] == "NO_RESULT"
    assert result["reset_may_have_run"] is True


# ----- concurrency and repeats -----------------------------------------------------------


def test_concurrent_requests_run_the_wrapper_once(platform, clock):
    import threading

    started, release = threading.Event(), threading.Event()
    jtag = kr260.Kr260Jtag(platform, config(), clock)
    engine = Engine(platform, kr260=jtag)

    def slow_run(*arguments):
        platform.wrapper_calls.append(arguments)
        started.set()
        release.wait(5)
        return (0, RST_ISSUED)

    platform.run_wrapper = slow_run
    results = {}
    first = threading.Thread(
        target=lambda: results.setdefault("first", engine.invoke(kr260.RESET, {}))
    )
    first.start()
    assert started.wait(5)
    results["second"] = engine.invoke(kr260.RESET, {})
    release.set()
    first.join(5)
    assert results["first"]["success"] is True
    assert results["second"]["error"] == "busy"
    assert len(platform.wrapper_calls) == 1


def test_there_is_no_fallback_or_second_run_after_an_xsdb_error(platform, clock):
    platform.wrapper_outcome = (
        1,
        "2026-10-29T03:00:00Z RESULT RST_ERROR at=2026-10-29T03:00:00.000Z boom\n",
    )
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert result["jtag_result"] == "RST_ERROR"
    assert len(platform.wrapper_calls) == 1
    assert platform.wrapper_calls[0][1][:2] == ["-Mode", "por"]


def test_wrapper_arguments_never_name_another_reset():
    import inspect

    source = inspect.getsource(kr260)
    assert "rst -system" not in source
    assert '"system"' not in source


def test_a_success_code_with_no_reset_time_is_still_only_a_request(platform, clock):
    # XSDB said RST_ISSUED: the agent reports exactly that, never "reset done".
    platform.wrapper_outcome = (0, RST_ISSUED)
    result = engine_for(platform, clock).invoke(kr260.RESET, {})
    assert set(result) & {"confirmed", "reset_confirmed", "booted"} == set()
