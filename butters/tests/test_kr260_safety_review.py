"""The durable KR260 safety-state review: write-ahead, file reliability, drift.

Invariants:
- an action that may restart the KR260 is recorded before it is sent;
- nothing that can fail, crash or race makes a wait end earlier than the
  policy allows;
- no clock error the trust check can miss may end a wait early.
"""

from __future__ import annotations

import errno
import os
import stat
import threading

import pytest
from test_kr260_clock_trust import BOOT_WALL, Clocks
from test_kr260_jtag_recovery import _dark_for
from test_kr260_management import Broker
from test_kr260_safety_state import BOOT_B, _established, _trust, _with_events

from butters.integrations import clock_trust, kr260_jtag
from butters.integrations.actions import Kr260Adapter
from butters.integrations.clock_trust import (
    MAX_UNDETECTED_SECONDS,
    RESTART_SYNC_ALLOWANCE_SECONDS,
    STEP_SECONDS,
)
from butters.integrations.model import IntegrationError
from butters.integrations.safety_state import SafetyRecord

EMPTY = {"kexec_at": None, "reset_at": None}
EVENTS = "kr260-recovery-events.json"


def _events(tmp_path):
    return SafetyRecord(tmp_path / EVENTS).read()


class RecordingActions:
    """A broker that notes the durable record at the moment of dispatch."""

    def __init__(self, path, outcome=None):
        self.path = path
        self.outcome = outcome
        self.seen = []

    def execute(self, operation, *, cancel_event=None):
        self.seen.append(SafetyRecord(self.path).read())
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {"success": True, "operation": str(operation)}


class RecordingHub:
    configured = True

    def __init__(self, path, outcome=None):
        self.path = path
        self.outcome = outcome
        self.seen = []
        self.calls = []

    def status(self):
        return {"state": "connected"}

    def supports(self, action):
        return True

    def kr260_jtag(self, action, *, cancel=None, idempotency_key=None):
        self.calls.append(action)
        self.seen.append(SafetyRecord(self.path).read())
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if isinstance(self.outcome, dict):
            return {"action": action, **self.outcome}
        return {
            "action": action,
            "success": True,
            "jtag_result": "RST_ISSUED",
            "reset_may_have_run": True,
        }


def _kexec_adapter(tmp_path, outcome=None, history=EMPTY):
    adapter, _hub, _broker, clock, wall = _with_events(
        tmp_path, broker=Broker(), history=history
    )
    adapter.actions = RecordingActions(tmp_path / EVENTS, outcome)
    return adapter, clock, wall


def _reset_adapter(tmp_path, outcome=None, history=EMPTY):
    adapter, _hub, _broker, clock, wall = _with_events(tmp_path, history=history)
    hub = RecordingHub(tmp_path / EVENTS, outcome)
    adapter.attach_jtag(hub)
    _dark_for(adapter, clock, 300)
    return adapter, hub, clock, wall


# ----- write-ahead ------------------------------------------------------------------


def test_a_kexec_request_is_recorded_before_it_is_sent(tmp_path):
    adapter, _clock, wall = _kexec_adapter(tmp_path)
    adapter.reboot()
    (at_dispatch,) = adapter.actions.seen
    assert at_dispatch["kexec_at"] == pytest.approx(wall["now"])


def test_a_reset_is_recorded_before_it_is_sent(tmp_path):
    adapter, hub, _clock, wall = _reset_adapter(tmp_path)
    adapter.jtag_reset()
    (at_dispatch,) = hub.seen
    assert at_dispatch["reset_at"] == pytest.approx(wall["now"])


def test_a_check_only_attaches_and_records_nothing(tmp_path):
    adapter, hub, _clock, _wall = _reset_adapter(
        tmp_path, outcome={"success": True, "jtag_result": "CHECK_OK"}
    )
    adapter.jtag_check()
    assert hub.seen == [EMPTY]
    assert _events(tmp_path) == EMPTY


@pytest.mark.parametrize("operation", ["kexec", "reset"])
def test_an_unwritable_record_refuses_before_anything_is_sent(tmp_path, operation):
    if operation == "kexec":
        adapter, _clock, _wall = _kexec_adapter(tmp_path)
    else:
        adapter, hub, _clock, _wall = _reset_adapter(tmp_path)
    os.chmod(tmp_path, 0o500)  # read-only state directory
    try:
        with pytest.raises(IntegrationError) as error:
            adapter.reboot() if operation == "kexec" else adapter.jtag_reset()
    finally:
        os.chmod(tmp_path, 0o700)
    assert error.value.code == "safety_state_unavailable"
    if operation == "kexec":
        assert adapter.actions.seen == []
        assert adapter._last_reboot is None
    else:
        assert hub.calls == []
        assert adapter._last_jtag is None
    assert "safety_state_unavailable" in Kr260Adapter.RESTART_NOT_SENT
    assert "safety_state_unavailable" in kr260_jtag.RESET_NOT_SENT


@pytest.mark.parametrize("operation", ["kexec", "reset"])
def test_a_crash_after_recording_and_before_sending_keeps_the_wait(tmp_path, operation):
    crash = SystemExit("Butters killed")
    if operation == "kexec":
        adapter, _clock, wall = _kexec_adapter(tmp_path, outcome=crash)
        with pytest.raises(SystemExit):
            adapter.reboot()
        key = "kexec_at"
    else:
        adapter, _hub, _clock, wall = _reset_adapter(tmp_path, outcome=crash)
        with pytest.raises(SystemExit):
            adapter.jtag_reset()
        key = "reset_at"
    assert _events(tmp_path)[key] == pytest.approx(wall["now"])


@pytest.mark.parametrize(
    "outcome",
    [
        IntegrationError("timeout", "no reply"),
        IntegrationError("ssh_failed", "lost"),
        IntegrationError("internal_error", "?"),
    ],
)
def test_a_lost_kexec_reply_never_clears_the_record(tmp_path, outcome):
    adapter, _clock, wall = _kexec_adapter(tmp_path, outcome=outcome)
    with pytest.raises(IntegrationError):
        adapter.reboot()
    assert _events(tmp_path)["kexec_at"] == pytest.approx(wall["now"])
    assert adapter._last_reboot is not None


@pytest.mark.parametrize(
    "outcome",
    [
        ConnectionError("agent lost"),
        {"success": False, "error": "timeout"},
        {"success": False, "error": "agent_unavailable_after_send"},
        {
            "success": False,
            "error": "jtag_failed",
            "jtag_result": "NO_RESULT",
            "reset_may_have_run": True,
        },
    ],
)
def test_a_lost_reset_reply_never_clears_the_record(tmp_path, outcome):
    adapter, _hub, _clock, wall = _reset_adapter(tmp_path, outcome=outcome)
    with pytest.raises(IntegrationError):
        adapter.jtag_reset()
    assert _events(tmp_path)["reset_at"] == pytest.approx(wall["now"])


def test_a_definite_refusal_puts_back_the_previous_cooldown(tmp_path):
    earlier = 1_000.0  # an old, valid reset
    adapter, hub, _clock, _wall = _reset_adapter(
        tmp_path,
        outcome={
            "success": False,
            "error": "jtag_failed",
            "jtag_result": "REFUSED_CHAIN",
            "reset_may_have_run": False,
        },
        history={"kexec_at": None, "reset_at": earlier},
    )
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "jtag_refused_chain"
    assert hub.seen[0]["reset_at"] > earlier  # written ahead...
    assert _events(tmp_path)["reset_at"] == earlier  # ...and put back, not erased


def test_a_definite_kexec_refusal_puts_back_no_event(tmp_path):
    adapter, _clock, _wall = _kexec_adapter(
        tmp_path, outcome=IntegrationError("identity_mismatch", "other host")
    )
    with pytest.raises(IntegrationError):
        adapter.reboot()
    assert adapter.actions.seen[0]["kexec_at"] is not None
    assert _events(tmp_path)["kexec_at"] is None
    assert adapter._last_reboot is None


def test_a_refusal_never_undoes_a_newer_event(tmp_path):
    # Another writer records a newer reset while this request is in flight.
    adapter, hub, _clock, wall = _reset_adapter(
        tmp_path,
        outcome={
            "success": False,
            "error": "board_reachable",
            "reset_may_have_run": False,
        },
    )
    original = hub.kr260_jtag

    def racing(*args, **kwargs):
        SafetyRecord(tmp_path / EVENTS).write(
            {"kexec_at": None, "reset_at": wall["now"] + 60}
        )
        return original(*args, **kwargs)

    hub.kr260_jtag = racing
    with pytest.raises(IntegrationError):
        adapter.jtag_reset()
    assert _events(tmp_path)["reset_at"] == wall["now"] + 60


def test_a_failed_undo_leaves_the_longer_wait(tmp_path):
    adapter, hub, _clock, wall = _reset_adapter(
        tmp_path,
        outcome={
            "success": False,
            "error": "board_reachable",
            "reset_may_have_run": False,
        },
    )
    original = hub.kr260_jtag

    def then_read_only(*args, **kwargs):
        os.chmod(tmp_path, 0o500)
        return original(*args, **kwargs)

    hub.kr260_jtag = then_read_only
    try:
        with pytest.raises(IntegrationError):
            adapter.jtag_reset()
    finally:
        os.chmod(tmp_path, 0o700)
    assert _events(tmp_path)["reset_at"] == pytest.approx(wall["now"])


def test_concurrent_writers_keep_the_newest_time(tmp_path):
    first, _c1, wall = _kexec_adapter(tmp_path)
    second, _c2, _w2 = _kexec_adapter(tmp_path, history=None)
    second.clock = lambda: wall["now"] - 300  # a writer with an older time
    first.reboot()
    second._last_reboot = None
    second.reboot()
    assert _events(tmp_path)["kexec_at"] == pytest.approx(wall["now"])


# ----- the files ------------------------------------------------------------------------


def test_threads_updating_one_record_lose_nothing(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"n": 0})

    def bump():
        for _ in range(40):
            record.update(lambda body: {"n": body["n"] + 1})

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert record.read() == {"n": 320}
    assert sorted(p.name for p in tmp_path.iterdir()) == [".r.json.lock", "r.json"]


def test_short_writes_are_completed(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data[:3])))
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"reset_at": 1_793_000_000.5, "note": "x" * 200})
    monkeypatch.undo()
    assert record.read() == {"reset_at": 1_793_000_000.5, "note": "x" * 200}


def test_a_full_disk_leaves_the_previous_record_and_no_temporary(tmp_path, monkeypatch):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"reset_at": 1.0})

    def full(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", full)
    with pytest.raises(OSError):
        record.write({"reset_at": 2.0})
    monkeypatch.undo()
    assert record.read() == {"reset_at": 1.0}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.json"]


def test_a_read_only_directory_can_be_read_but_not_written(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"a": 1})
    os.chmod(tmp_path, 0o500)
    try:
        assert record.read() == {"a": 1}
        with pytest.raises(OSError):
            record.update(lambda body: {"a": 2})
    finally:
        os.chmod(tmp_path, 0o700)
    assert record.read() == {"a": 1}


def test_the_file_and_its_directory_are_flushed(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd) or real(fd))
    SafetyRecord(tmp_path / "r.json").write({"a": 1})
    assert len(synced) == 2  # the temporary file, then the directory


def test_the_record_and_its_lock_are_private(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    record.update(lambda body: {"a": 1})
    for name in ("r.json", ".r.json.lock"):
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600


def test_an_older_valid_record_is_accepted_the_checksum_is_not_authentication(tmp_path):
    # Documented limitation: restoring an old copy of the state directory
    # restores old times. The checksum only detects accidental damage.
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"reset_at": 1.0})
    old = record.path.read_bytes()
    record.write({"reset_at": 2.0})
    record.path.write_bytes(old)
    assert record.read() == {"reset_at": 1.0}


def test_first_start_with_both_files_absent_is_fail_closed_and_status_works(tmp_path):
    adapter, _hub, _broker, clock, _wall = _with_events(tmp_path)
    clocks = Clocks()
    clocks.sync()
    adapter.clock_trusted = _trust(clocks, tmp_path)  # no baseline file yet
    _dark_for(adapter, clock, 300)
    summary = adapter.status()["jtag_recovery"]
    assert summary["reset_blocked_reason"] == "clock_unsynchronized"
    assert _events(tmp_path) is None  # nothing written by an untrusted clock
    # The next sync this process sees establishes the baseline; the unknown
    # history then costs one interval.
    clocks.advance(60)
    clocks.sync()
    summary = adapter.status()["jtag_recovery"]
    assert summary["reset_blocked_reason"] == "restart_recent"
    assert _events(tmp_path)["reason"] == "history_unknown"


# ----- clock drift -------------------------------------------------------------------------


def test_a_long_outage_does_not_widen_the_tolerance(tmp_path):
    # Thirty days without NTP: no sync, so wall and boot time cannot diverge.
    clocks = _established(tmp_path)
    clocks.advance(30 * 86_400)
    clocks.step(5.0)
    assert _trust(clocks, tmp_path)() is False


def test_a_step_within_the_tolerance_is_covered_by_the_margin(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(600)
    clocks.step(STEP_SECONDS * 0.9)  # undetectable
    assert _trust(clocks, tmp_path)() is True
    assert MAX_UNDETECTED_SECONDS >= STEP_SECONDS


def test_small_steps_across_restarts_accumulate_and_are_caught(tmp_path):
    clocks = _established(tmp_path)
    baseline = (tmp_path / "baseline.json").read_bytes()
    results = []
    for _ in range(3):
        clocks.advance(60)
        clocks.step(STEP_SECONDS * 0.75)
        results.append(_trust(clocks, tmp_path)())
    assert results == [True, False, False]
    # Restarts without a sync never move the reference.
    assert (tmp_path / "baseline.json").read_bytes() == baseline


def test_a_large_correction_by_a_missed_sync_waits_for_a_live_sync(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(20 * 86_400)
    # The oscillator drifted while offline, and NTP stepped the clock back 60 s
    # while Butters was down (a correction, touching the marker).
    clocks.step(-60.0)
    clocks.marker_mtime = clocks.wall
    restarted = _trust(clocks, tmp_path)
    assert restarted() is False  # beyond the restart allowance: not trusted blind
    clocks.advance(2048)
    clocks.marker_mtime = clocks.wall  # the next poll, seen live, no further step
    assert restarted() is True


def test_a_large_correction_seen_live_is_trusted_and_rebaselined(tmp_path):
    clocks = _established(tmp_path)
    running = _trust(clocks, tmp_path)
    assert running() is True
    clocks.advance(20 * 86_400)
    # NTP back: it steps the clock by 90 s of drift and touches the marker.
    clocks.step(90.0)
    clocks.marker_mtime = clocks.wall
    assert running() is True
    assert _trust(clocks, tmp_path)() is True  # and a restart after it


def test_a_live_sync_clears_a_stored_step(tmp_path):
    clocks = _established(tmp_path)
    running = _trust(clocks, tmp_path)
    assert running() is True
    clocks.advance(30)
    clocks.step(600)
    assert running() is False
    clocks.sync()
    assert running() is True
    assert SafetyRecord(tmp_path / "baseline.json").read()["stepped"] is False


def test_a_pi_reboot_with_restored_old_time_waits_for_ntp(tmp_path):
    _established(tmp_path)
    rebooted = Clocks(behind=3600)  # timesyncd restored a time an hour old
    trust = _trust(rebooted, tmp_path, boot=BOOT_B)
    assert trust() is False  # no marker yet
    rebooted.advance(40)
    rebooted.offset = BOOT_WALL + 3 * 3600  # NTP: three hours after the old boot
    rebooted.marker_mtime = rebooted.wall
    assert trust() is True  # a sync this process saw


def test_an_undetected_error_cannot_end_a_wait_early(tmp_path):
    # A reset stored by a correct clock; then the largest error the check can
    # miss. The wait still lasts the full interval.
    adapter, _hub, _broker, clock, wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": 1_793_000_000.0}
    )
    wall["now"] = 1_793_000_000.0 + kr260_jtag.POR_INTERVAL_SECONDS - 1
    wall["now"] += MAX_UNDETECTED_SECONDS  # the clock runs ahead, undetected
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"


def test_the_margin_covers_every_undetectable_error():
    assert MAX_UNDETECTED_SECONDS >= RESTART_SYNC_ALLOWANCE_SECONDS + STEP_SECONDS
    assert MAX_UNDETECTED_SECONDS < kr260_jtag.KEXEC_QUIET_SECONDS / 10
    assert clock_trust.DRIFT_PER_SECOND * 2048 + STEP_SECONDS < (
        RESTART_SYNC_ALLOWANCE_SECONDS
    )  # a normal missed poll fits under the cap
