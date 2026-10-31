"""Durable KR260 safety state: the clock baseline and the recovery events.

Invariants:
- an untrusted clock change, even across a Butters or Pi restart, never makes
  recovery eligible early;
- missing or corrupt safety history never makes a reset (or a check after a
  kexec request) eligible earlier than the policy permits.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_kr260_clock_trust import BOOT_WALL, Clocks
from test_kr260_jtag_recovery import _adapter, _dark_for
from test_kr260_management import Broker

from butters.integrations import kr260_jtag
from butters.integrations.clock_trust import ClockTrust
from butters.integrations.model import IntegrationError
from butters.integrations.safety_state import CORRUPT, SafetyRecord

BOOT_A = "11111111-1111-1111-1111-111111111111"
BOOT_B = "22222222-2222-2222-2222-222222222222"


# ----- the record ---------------------------------------------------------------------


def test_a_record_round_trips_privately_and_atomically(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    assert record.read() is None
    record.write({"a": 1.5, "b": None})
    assert record.read() == {"a": 1.5, "b": None}
    assert oct(os.stat(record.path).st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.json"]  # no temp left


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b'{"schema": 1, "body": {"a": 1}, "sha256": "0"}',
        b'{"schema": 2, "body": {}, "sha256": ""}',
        b'{"schema": 1, "body": [], "sha256": ""}',
        b"\xff\xfe",
    ],
)
def test_a_damaged_record_reads_as_corrupt(tmp_path, content):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"a": 1})
    record.path.write_bytes(content)
    assert record.read() is CORRUPT


def test_a_truncated_or_edited_record_fails_its_checksum(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"reset_at": 1000.0})
    document = json.loads(record.path.read_text())
    document["body"]["reset_at"] = 1.0  # made older by hand
    record.path.write_text(json.dumps(document))
    assert record.read() is CORRUPT
    record.path.write_bytes(record.path.read_bytes()[:-7])
    assert record.read() is CORRUPT


def test_a_leftover_temporary_file_does_not_affect_the_record(tmp_path):
    record = SafetyRecord(tmp_path / "r.json")
    record.write({"a": 1})
    (tmp_path / ".r.json.12345.tmp").write_text("partial")
    assert record.read() == {"a": 1}


def test_an_unreadable_record_is_corrupt(tmp_path):
    (tmp_path / "r.json").mkdir()
    assert SafetyRecord(tmp_path / "r.json").read() is CORRUPT


# ----- the clock baseline across restarts and reboots ---------------------------------


def _trust(clocks, tmp_path, boot=BOOT_A):
    return ClockTrust(
        stat=clocks.stat,
        wall=lambda: clocks.wall,
        monotonic=lambda: clocks.mono,
        boottime=lambda: clocks.mono,
        baseline=SafetyRecord(tmp_path / "baseline.json"),
        boot_id=lambda: boot,
    )


def _established(tmp_path):
    """A synced boot whose baseline a running Butters has written."""

    clocks = Clocks()
    clocks.sync()
    first = _trust(clocks, tmp_path)
    assert first() is False  # no baseline yet: wait for a sync this process sees
    clocks.advance(60)
    clocks.sync()
    assert first() is True
    assert SafetyRecord(tmp_path / "baseline.json").read()["boot_id"] == BOOT_A
    return clocks


def test_a_missing_baseline_waits_for_a_sync_this_process_sees(tmp_path):
    _established(tmp_path)


def test_a_plain_restart_is_trusted_at_once(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(300)
    assert _trust(clocks, tmp_path)() is True


@pytest.mark.parametrize("step", [86_400.0, 600.0, 5.0, -5.0, -600.0])
def test_a_step_just_before_a_restart_is_caught_by_the_new_process(tmp_path, step):
    clocks = _established(tmp_path)
    clocks.advance(120)
    clocks.step(step)  # Butters was down, or restarted right after
    restarted = _trust(clocks, tmp_path)
    assert restarted() is False
    clocks.advance(60)
    assert restarted() is False
    clocks.sync()  # NTP puts the clock right and touches the marker
    assert restarted() is True


def test_a_step_seen_in_process_stays_latched_across_a_restart(tmp_path):
    clocks = _established(tmp_path)
    running = _trust(clocks, tmp_path)
    assert running() is True
    clocks.advance(30)
    clocks.step(3600)
    assert running() is False
    clocks.step(-3600)  # put back by hand before the restart
    clocks.advance(30)
    assert _trust(clocks, tmp_path)() is False
    clocks.sync()
    assert _trust(clocks, tmp_path)() is True


def test_a_sync_while_butters_was_down_is_accepted(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(3 * 3600)
    clocks.sync()  # a routine poll; no step
    assert _trust(clocks, tmp_path)() is True


def test_a_step_after_a_sync_while_butters_was_down_is_caught(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(3600)
    clocks.sync()
    clocks.advance(60)
    clocks.step(600)
    assert _trust(clocks, tmp_path)() is False


def test_a_new_boot_with_a_synchronized_clock_is_trusted(tmp_path):
    _established(tmp_path)
    rebooted = Clocks()
    rebooted.offset += 7200  # two hours later
    rebooted.sync()
    rebooted.offset = BOOT_WALL + 7200
    rebooted.marker_mtime = rebooted.wall
    assert _trust(rebooted, tmp_path, boot=BOOT_B)() is True
    assert SafetyRecord(tmp_path / "baseline.json").read()["boot_id"] == BOOT_B


def test_a_new_boot_whose_clock_is_earlier_than_a_trusted_time_is_untrusted(tmp_path):
    clocks = _established(tmp_path)
    clocks.advance(3600)
    SafetyRecord(tmp_path / "baseline.json").write(
        {
            "boot_id": BOOT_A,
            "marker": clocks.marker_mtime,
            "wall": clocks.wall,
            "boottime": clocks.mono,
            "stepped": False,
        }
    )
    rebooted = Clocks()  # its "synchronized" clock reads an hour earlier
    rebooted.sync()
    assert _trust(rebooted, tmp_path, boot=BOOT_B)() is False


@pytest.mark.parametrize("damage", [b"", b"not json", b'{"schema": 1}'])
def test_a_corrupt_baseline_waits_for_the_next_sync(tmp_path, damage):
    clocks = _established(tmp_path)
    (tmp_path / "baseline.json").write_bytes(damage)
    restarted = _trust(clocks, tmp_path)
    assert restarted() is False
    clocks.advance(60)
    clocks.sync()
    assert restarted() is True


def test_a_baseline_with_malformed_fields_waits_for_the_next_sync(tmp_path):
    clocks = _established(tmp_path)
    SafetyRecord(tmp_path / "baseline.json").write(
        {"boot_id": BOOT_A, "marker": "x", "wall": True, "boottime": None}
    )
    assert _trust(clocks, tmp_path)() is False


def test_an_unreadable_boot_id_is_untrusted(tmp_path):
    clocks = _established(tmp_path)

    def broken():
        raise OSError("no /proc")

    trust = ClockTrust(
        stat=clocks.stat,
        wall=lambda: clocks.wall,
        monotonic=lambda: clocks.mono,
        boottime=lambda: clocks.mono,
        baseline=SafetyRecord(tmp_path / "baseline.json"),
        boot_id=broken,
    )
    assert trust() is False


def test_a_baseline_that_cannot_be_written_is_untrusted(tmp_path):
    clocks = Clocks()
    clocks.sync()
    trust = ClockTrust(
        stat=clocks.stat,
        wall=lambda: clocks.wall,
        monotonic=lambda: clocks.mono,
        boottime=lambda: clocks.mono,
        baseline=SafetyRecord(tmp_path / "missing-dir" / "baseline.json"),
        boot_id=lambda: BOOT_A,
    )
    assert trust() is False
    clocks.advance(60)
    clocks.sync()
    assert trust() is False  # still unprotected across a restart


# ----- the recovery events across lost job history -------------------------------------


def _with_events(tmp_path, *, broker=None, history=None):
    adapter, hub, broker, clock = _adapter(broker=broker)
    wall = {"now": BOOT_WALL}
    adapter.clock = lambda: wall["now"]
    adapter.attach_safety_state(tmp_path)
    adapter.clock_trusted = lambda: True  # the clock is tested above
    if history is not None:
        SafetyRecord(tmp_path / "kr260-recovery-events.json").write(history)
    return adapter, hub, broker, clock, wall


def _events(tmp_path):
    return SafetyRecord(tmp_path / "kr260-recovery-events.json").read()


def _advance(clock, wall, seconds):
    clock.now += seconds
    wall["now"] += seconds


def test_a_known_empty_history_adds_no_wait(tmp_path):
    adapter, hub, _broker, clock, _wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": None}
    )
    _dark_for(adapter, clock, 300)
    assert adapter.jtag_reset()["outcome"] == "reset_requested"
    assert len(hub.calls) == 1


def test_a_missing_history_costs_one_full_interval(tmp_path):
    adapter, hub, _broker, clock, wall = _with_events(tmp_path)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_check()
    assert error.value.code == "restart_recent"
    stored = _events(tmp_path)
    assert stored["reason"] == "history_unknown"
    assert stored["kexec_at"] == stored["reset_at"] == pytest.approx(wall["now"])
    _advance(clock, wall, kr260_jtag.KEXEC_QUIET_SECONDS + 1)
    adapter.status(refresh=True)
    assert adapter.jtag_check()["outcome"] == "check_passed"
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    _advance(clock, wall, kr260_jtag.POR_INTERVAL_SECONDS)
    adapter.status(refresh=True)
    assert adapter.jtag_reset()["outcome"] == "reset_requested"
    # Writing it down once means a restart does not start the wait again.
    assert len(hub.calls) == 2


@pytest.mark.parametrize(
    "damage", [b"", b"{garbage", b'{"schema": 1, "body": {}, "sha256": "x"}']
)
def test_a_corrupt_history_is_treated_as_lost(tmp_path, damage):
    adapter, hub, _broker, clock, _wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": None}
    )
    (tmp_path / "kr260-recovery-events.json").write_bytes(damage)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "restart_recent"
    assert _events(tmp_path)["reason"] == "history_unknown"
    assert hub.calls == []


def test_a_reset_survives_the_loss_of_the_job_store(tmp_path):
    adapter, _hub, _broker, clock, wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": None}
    )
    _dark_for(adapter, clock, 300)
    adapter.jtag_reset()
    assert _events(tmp_path)["reset_at"] == pytest.approx(wall["now"])
    # Butters restarts and the job store is gone (the persisted lookups return
    # nothing); the durable record still holds the reset.
    later, later_hub, _b, later_clock, _later_wall = _with_events(tmp_path)
    later.clock = lambda: wall["now"] + 600
    _dark_for(later, later_clock, 300)
    with pytest.raises(IntegrationError) as error:
        later.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert later_hub.calls == []


def test_a_kexec_request_survives_the_loss_of_the_job_store(tmp_path):
    adapter, _hub, _broker, _clock, wall = _with_events(
        tmp_path, broker=Broker(), history={"kexec_at": None, "reset_at": None}
    )
    adapter.reboot()
    assert _events(tmp_path)["kexec_at"] == pytest.approx(wall["now"])
    later, later_hub, later_broker, later_clock, _w = _with_events(tmp_path)
    later.clock = lambda: wall["now"] + 120
    later_broker.ok, later_broker.error_code = False, "host_unreachable"
    _dark_for(later, later_clock, 300)
    with pytest.raises(IntegrationError) as error:
        later.jtag_check()
    assert error.value.code == "restart_recent"
    assert later_hub.calls == []


def test_the_later_of_the_job_store_and_the_record_counts(tmp_path):
    adapter, hub, _broker, clock, wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": BOOT_WALL - 3600}
    )
    adapter.attach_jtag(hub, persisted_reset_at=lambda: wall["now"] - 60)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    # And the other way round: an old job store does not hide a newer record.
    adapter.attach_jtag(hub, persisted_reset_at=lambda: wall["now"] - 7200)
    SafetyRecord(tmp_path / "kr260-recovery-events.json").write(
        {"kexec_at": None, "reset_at": wall["now"] - 60}
    )
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []


@pytest.mark.parametrize("value", ["1000", True, float("nan"), float("-inf"), [1]])
def test_a_malformed_event_time_counts_as_just_now(tmp_path, value):
    adapter, hub, _broker, clock, _wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": value}
    )
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []


def test_an_untrusted_clock_never_writes_the_conservative_record(tmp_path):
    # "Just now" by a clock that may be a day behind would later look old.
    adapter, hub, _broker, clock, _wall = _with_events(tmp_path)
    adapter.clock_trusted = lambda: False
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "clock_unsynchronized"
    assert _events(tmp_path) is None
    assert hub.calls == []


def test_recording_into_a_lost_record_keeps_the_other_wait(tmp_path):
    adapter, _hub, _broker, clock, wall = _with_events(
        tmp_path, history={"kexec_at": None, "reset_at": None}
    )
    _dark_for(adapter, clock, 300)
    (tmp_path / "kr260-recovery-events.json").write_bytes(b"lost")
    adapter._record_event("reset_at")
    stored = _events(tmp_path)
    # The kexec time was unknown too, so it is recorded as just now.
    assert stored["kexec_at"] == stored["reset_at"] == pytest.approx(wall["now"])


def test_a_record_that_cannot_be_written_never_breaks_recovery_or_status(tmp_path):
    adapter, hub, _broker, clock, _wall = _with_events(tmp_path)
    adapter._events = SafetyRecord(tmp_path / "no-such-dir" / "events.json")
    _dark_for(adapter, clock, 300)
    assert adapter.status()["jtag_recovery"]["reset_blocked_reason"] == "restart_recent"
    with pytest.raises(IntegrationError):
        adapter.jtag_reset()
    assert hub.calls == []


def test_the_service_attaches_both_records_in_its_state_directory(tmp_path):
    from test_kr260_admin_api import _application

    _app, service, _nas = _application(tmp_path)
    adapter = service.assistant.kr260_adapter
    assert isinstance(adapter.clock_trusted, ClockTrust)
    assert (
        adapter.clock_trusted.baseline.path
        == Path(service.state_dir) / "kr260-clock-baseline.json"
    )
    assert (
        adapter._events.path == Path(service.state_dir) / "kr260-recovery-events.json"
    )


@pytest.mark.parametrize("trusted", [True, False])
def test_the_first_look_at_a_lost_history_already_waits(tmp_path, trusted):
    adapter, _hub, _broker, _clock, wall = _with_events(tmp_path)
    times = adapter._event_times(trusted)
    assert times == {"kexec_at": wall["now"], "reset_at": wall["now"]}
