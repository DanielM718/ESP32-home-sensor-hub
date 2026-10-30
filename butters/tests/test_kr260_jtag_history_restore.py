"""The Admin page's last JTAG attempt, rebuilt from the job store after a restart.

Display only: the reset interval and the post-kexec quiet period come from
their own persisted lookups (tests/test_kr260_jtag_recovery.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_kr260_jtag_recovery import NEW_BOOT, _adapter, _dark_for
from test_kr260_management import Broker

from butters.assistant_config import load_assistant_settings

OLD_BOOT = "63bb230d-e96c-438c-917a-dcc464a80a44"
WALL = 50_000.0


def _job(skill="reset_kr260_jtag", state="completed", age=100.0, **fields):
    return {
        "skill": skill,
        "state": state,
        "created_at": WALL - age,
        "result": fields.pop("result", None),
        "failure_code": fields.pop("failure_code", None),
        **fields,
    }


def _restoring(job, *, broker=None):
    adapter, hub, broker, clock = _adapter(broker=broker)
    adapter.clock = lambda: WALL
    calls = []

    def lookup():
        calls.append(1)
        return job() if callable(job) else job

    adapter.attach_jtag(hub, persisted_attempt=lookup)
    return adapter, hub, broker, clock, calls


def test_a_confirmed_reset_from_before_the_restart_is_shown_confirmed():
    job = _job(
        result={
            "success": True,
            "jtag_result": "RST_ISSUED",
            "reset_issued_at": "2026-10-29T03:26:40.012Z",
            "previous_boot_id": OLD_BOOT,
            "outcome": "reset_requested",
        }
    )
    adapter, hub, _broker, _clock, _calls = _restoring(
        job, broker=Broker(output=NEW_BOOT)
    )
    attempt = adapter.status(refresh=True)["jtag_recovery"]["last_attempt"]
    assert attempt["operation"] == "reset"
    assert attempt["state"] == "confirmed"
    assert attempt["evidence"] == "new_boot_id"
    assert attempt["restored"] is True
    assert attempt["jtag_result"] == "RST_ISSUED"
    assert attempt["reset_issued_at"] == "2026-10-29T03:26:40.012Z"
    assert attempt["previous_boot_id"] == OLD_BOOT
    assert attempt["age_seconds"] == pytest.approx(100.0, abs=1)
    assert hub.calls == []


def test_a_reset_refused_before_sending_is_shown_failed_not_sent():
    adapter, *_ = _restoring(_job(state="failed", failure_code="jtag_refused_chain"))
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["state"] == "failed"
    assert attempt["evidence"] == "not_sent"
    assert attempt["error"] == "jtag_refused_chain"
    assert attempt["jtag_result"] == "REFUSED_CHAIN"


def test_an_interrupted_reset_is_shown_as_possibly_sent_and_unconfirmed():
    # Butters restarted mid-request: the job store marks it, and nothing proves
    # a new boot from a dark board.
    adapter, *_ = _restoring(
        _job(state="failed", failure_code="restart_recovery_required", age=900)
    )
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["state"] == "unconfirmed"
    assert attempt["evidence"] == "no_answer"
    assert attempt["error"] == "restart_recovery_required"
    assert attempt["jtag_result"] is None


def test_a_passed_check_is_shown_as_a_check():
    adapter, *_ = _restoring(
        _job(
            skill="check_kr260_jtag",
            result={"success": True, "jtag_result": "CHECK_OK"},
        )
    )
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert (attempt["operation"], attempt["state"]) == ("check", "check_passed")
    assert attempt["restored"] is True


def test_the_store_is_read_once_and_a_new_attempt_replaces_the_restored_one():
    adapter, hub, _broker, clock, calls = _restoring(
        _job(state="failed", failure_code="jtag_refused_chain")
    )
    for _ in range(3):
        adapter.status()
    assert len(calls) == 1
    _dark_for(adapter, clock, 300)
    adapter.jtag_check()
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["restored"] is False
    assert attempt["state"] == "check_passed"
    assert len(calls) == 1
    assert len(hub.calls) == 1


@pytest.mark.parametrize(
    "job",
    [
        _job(skill="reboot_kr260"),
        _job(state="running"),
        _job(created_at="yesterday"),
        _job(created_at=float("nan")),
        _job(created_at=True),
    ],
)
def test_an_unrecognised_record_shows_no_last_attempt(job):
    adapter, *_ = _restoring(job)
    assert adapter.status()["jtag_recovery"]["last_attempt"] is None


def test_an_unreadable_store_shows_no_last_attempt_and_keeps_status_working():
    def broken():
        import sqlite3

        raise sqlite3.OperationalError("database is locked")

    adapter, *_ = _restoring(broken)
    status = adapter.status()
    assert status["jtag_recovery"]["last_attempt"] is None
    assert status["jtag_recovery"]["board"] in {"unreachable", "unreachable_confirmed"}


def test_an_unknown_wrapper_code_is_not_shown():
    adapter, *_ = _restoring(_job(state="failed", failure_code="jtag_made_up"))
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["jtag_result"] is None
    assert attempt["error"] == "jtag_made_up"
    # Unknown failure: possibly sent, so never "failed (not sent)".
    assert attempt["state"] != "failed"


def test_a_restored_attempt_never_decides_a_refusal():
    # Only the persisted interval lookups refuse; a restored row does not.
    adapter, _hub, _broker, clock, _calls = _restoring(_job(age=60))
    _dark_for(adapter, clock, 300)
    summary = adapter.status()["jtag_recovery"]
    assert summary["last_attempt"]["restored"] is True
    assert summary["reset_blocked_reason"] is None
    assert adapter.jtag_reset()["outcome"] == "reset_requested"


def test_store_returns_the_newest_finished_jtag_job(tmp_path):
    from butters.actions.store import ActionStateStore

    store = ActionStateStore(
        tmp_path / "actions.sqlite3",
        load_assistant_settings().actions,
        clock=lambda: 1.0,
    )

    def insert(job_id, skill, created, state, failure=None):
        with store._lock, store._connect() as connection:
            connection.execute(
                "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
                "created_at,state,stage,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, "p", skill, "s", "s", "i", created, state, state, failure),
            )

    skills = ("reset_kr260_jtag", "check_kr260_jtag")
    assert store.last_finished_job(skills) is None
    insert("a", "check_kr260_jtag", 10.0, "completed")
    insert("b", "reset_kr260_jtag", 20.0, "failed", "jtag_refused_chain")
    insert("c", "reboot_kr260", 30.0, "completed")
    insert("d", "reset_kr260_jtag", 40.0, "running")
    job = store.last_finished_job(skills)
    assert (job["job_id"], job["failure_code"]) == ("b", "jtag_refused_chain")


def test_the_service_wires_the_restored_attempt(tmp_path):
    from test_kr260_admin_api import _jtag_application

    _app, service, _hub = _jtag_application(tmp_path)
    adapter = service.assistant.kr260_adapter
    store = service.action_state
    with store._lock, store._connect() as connection:
        connection.execute(
            "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
            "created_at,state,stage,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "j",
                "p",
                "reset_kr260_jtag",
                "s",
                "s",
                "i",
                store.clock(),
                "failed",
                "failed",
                "jtag_refused_reachable",
            ),
        )
    attempt = adapter.status()["jtag_recovery"]["last_attempt"]
    assert attempt["restored"] is True
    assert attempt["state"] == "failed"
    assert attempt["jtag_result"] == "REFUSED_REACHABLE"


def test_the_admin_page_marks_a_restored_row():
    script = (
        Path(__file__).resolve().parents[1] / "src/butters/web/static/assets/admin.js"
    )
    assert 'attempt.restored?" · from job history":""' in script.read_text()
