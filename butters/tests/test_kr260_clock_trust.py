"""KR260 recovery never trusts a wall clock that may jump (no RTC on this Pi).

Invariant: an untrusted wall-clock change never makes a JTAG operation, or a
kexec restart, eligible earlier than the recovery policy permits.
"""

from __future__ import annotations

import math
import os
from dataclasses import replace
from pathlib import Path

import pytest
from test_kr260_jtag_recovery import _adapter, _dark_for
from test_kr260_management import TELEMETRY, Broker

from butters.assistant_config import load_assistant_settings
from butters.integrations import clock_trust, kr260_jtag
from butters.integrations.actions import Kr260Adapter
from butters.integrations.clock_trust import ClockTrust
from butters.integrations.model import IntegrationError

BOOT_WALL = 1_793_000_000.0  # wall-clock time of this boot, once synchronized


class Clocks:
    """A booted Pi: wall clock, monotonic clock, uptime and timesyncd's marker."""

    def __init__(self, *, behind: float = 0.0) -> None:
        self.mono = 100.0  # monotonic == uptime here
        self.offset = BOOT_WALL - behind  # wall = offset + mono
        self.marker_mtime: float | None = None
        self.marker_error: Exception | None = None

    @property
    def wall(self) -> float:
        return self.offset + self.mono

    def advance(self, seconds: float) -> None:
        self.mono += seconds

    def step(self, seconds: float) -> None:
        self.offset += seconds

    def sync(self) -> None:
        """NTP contact: the clock becomes correct and the marker is touched."""

        self.offset = BOOT_WALL
        self.marker_mtime = self.wall

    def stat(self, _path: Path) -> os.stat_result:
        if self.marker_error is not None:
            raise self.marker_error
        if self.marker_mtime is None:
            raise FileNotFoundError("no marker")
        return os.stat_result((0o100644, 0, 0, 1, 0, 0, 0, 0, self.marker_mtime, 0))

    def trust(self) -> ClockTrust:
        return ClockTrust(
            marker=Path("/run/systemd/timesync/synchronized"),
            stat=self.stat,
            wall=lambda: self.wall,
            monotonic=lambda: self.mono,
            boottime=lambda: self.mono,
        )


# ----- the trust check itself -----------------------------------------------------


def test_a_cold_boot_without_ntp_is_untrusted():
    clocks = Clocks(behind=86_400)
    trust = clocks.trust()
    assert trust() is False
    clocks.advance(3600)  # NTP stays unreachable
    assert trust() is False


def test_synchronization_after_a_delayed_start_becomes_trusted():
    clocks = Clocks(behind=86_400)
    trust = clocks.trust()
    assert trust() is False
    clocks.advance(40)
    clocks.sync()  # steps the clock forward a day; that is the sync itself
    assert trust() is True
    clocks.advance(60)
    assert trust() is True


def test_synchronized_then_offline_stays_trusted():
    clocks = Clocks()
    clocks.sync()
    trust = clocks.trust()
    assert trust() is True
    clocks.advance(6 * 3600)  # no further NTP contact; the marker ages
    assert trust() is True


@pytest.mark.parametrize("step", [3600.0, -3600.0, 3.0, -3.0])
def test_a_clock_step_after_sync_is_untrusted_until_the_next_sync(step):
    clocks = Clocks()
    clocks.sync()
    trust = clocks.trust()
    assert trust() is True
    clocks.advance(10)
    clocks.step(step)  # e.g. a manual `date -s`; timesyncd did not sync
    assert trust() is False
    clocks.step(-step)  # put back by hand: still latched
    clocks.advance(10)
    assert trust() is False
    clocks.sync()  # the next NTP contact touches the marker and re-anchors
    assert trust() is True


def test_slewing_within_the_tolerance_is_not_a_step():
    clocks = Clocks()
    clocks.sync()
    trust = clocks.trust()
    assert trust() is True
    clocks.advance(3600)
    clocks.step(clock_trust.STEP_SECONDS / 2)
    assert trust() is True


@pytest.mark.parametrize(
    "mtime_shift", [-(3 * clock_trust.MARKER_TOLERANCE_SECONDS) - 200.0, 3600.0]
)
def test_a_marker_from_before_this_boot_or_ahead_of_the_clock_is_untrusted(mtime_shift):
    clocks = Clocks()
    clocks.sync()
    # Before the boot started (BOOT_WALL) or in the future.
    clocks.marker_mtime = (
        BOOT_WALL + mtime_shift if mtime_shift < 0 else clocks.wall + mtime_shift
    )
    assert clocks.trust()() is False


def test_a_forward_step_larger_than_the_uptime_at_sync_is_caught_after_a_restart():
    # Synced 40 s after boot; the clock is then stepped a day forward and
    # Butters restarts before the next sync. The marker now appears to predate
    # the boot, so the new process does not trust the clock either.
    clocks = Clocks(behind=86_400)
    clocks.advance(40)
    clocks.sync()
    clocks.advance(600)
    clocks.step(86_400)
    assert clocks.trust()() is False


@pytest.mark.parametrize(
    "error", [PermissionError("denied"), OSError("io"), ValueError("weird")]
)
def test_an_unreadable_marker_is_untrusted(error):
    clocks = Clocks()
    clocks.sync()
    clocks.marker_error = error
    assert clocks.trust()() is False


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_malformed_marker_time_is_untrusted(bad):
    clocks = Clocks()
    clocks.sync()
    clocks.marker_mtime = bad
    assert clocks.trust()() is False


def test_unreadable_clocks_are_untrusted():
    def broken():
        raise OSError("clock_gettime failed")

    clocks = Clocks()
    clocks.sync()
    trust = ClockTrust(
        stat=clocks.stat,
        wall=lambda: clocks.wall,
        monotonic=lambda: clocks.mono,
        boottime=broken,
    )
    assert trust() is False


def test_the_default_marker_is_timesyncds(monkeypatch):
    monkeypatch.undo()  # conftest points it elsewhere for every test
    assert clock_trust.MARKER == Path("/run/systemd/timesync/synchronized")


def test_tests_never_see_this_machines_ntp_state():
    # tests/conftest.py: the default ClockTrust is untrusted under pytest.
    assert ClockTrust()() is False


# ----- the policy ------------------------------------------------------------------


def test_an_untrusted_clock_blocks_jtag_after_the_dark_checks():
    dark = kr260_jtag.DarkRun()
    common = {
        "last_kexec_at": None,
        "last_reset_at": None,
        "reset": True,
        "clock_trusted": False,
    }
    assert kr260_jtag.refusal(now=1000.0, dark=dark, **common) == "board_reachable"
    dark.since, dark.count = 900.0, 2
    assert (
        kr260_jtag.refusal(now=1000.0, dark=dark, **common) == "offline_not_established"
    )
    dark.since = 0.0
    for reset in (True, False):
        assert (
            kr260_jtag.refusal(now=1000.0, dark=dark, **{**common, "reset": reset})
            == "clock_unsynchronized"
        )


def test_clock_unsynchronized_is_a_known_reason_and_sends_nothing():
    assert "clock_unsynchronized" in kr260_jtag.BLOCKED_REASONS
    assert "clock_unsynchronized" in kr260_jtag.RESET_NOT_SENT
    assert "clock_unsynchronized" in Kr260Adapter.RESTART_NOT_SENT


# ----- the adapter -----------------------------------------------------------------


def _untrusted(adapter, value=False):
    adapter.clock_trusted = lambda: value
    return adapter


def test_an_untrusted_clock_refuses_jtag_but_monitoring_keeps_working():
    adapter, hub, _broker, clock = _adapter()
    _untrusted(adapter)
    _dark_for(adapter, clock, 300)
    status = adapter.status()
    summary = status["jtag_recovery"]
    assert summary["board"] == "unreachable_confirmed"
    assert summary["reset_blocked_reason"] == "clock_unsynchronized"
    assert summary["check_blocked_reason"] == "clock_unsynchronized"
    assert summary["reset_permitted"] is summary["check_permitted"] is False
    for run in (adapter.jtag_check, adapter.jtag_reset):
        with pytest.raises(IntegrationError) as error:
            run()
        assert error.value.code == "clock_unsynchronized"
    assert hub.calls == []
    # The dark period kept counting; once the clock is trusted, no new wait.
    adapter.clock_trusted = lambda: True
    assert adapter.jtag_reset()["outcome"] == "reset_requested"


def test_a_healthy_board_is_still_observed_with_fpga_telemetry_while_blocked():
    adapter, _hub, _broker, _clock = _adapter(broker=Broker(output=TELEMETRY))
    _untrusted(adapter)
    status = adapter.status(refresh=True)
    assert status["operational_state"] == "online"
    assert status["identity_verified"] is True
    assert "fpga" in status
    assert status["jtag_recovery"]["board"] == "online"
    # A reachable board is reported as such, whatever the clock.
    assert status["jtag_recovery"]["reset_blocked_reason"] == "board_reachable"


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_a_clock_check_that_is_not_exactly_true_is_untrusted(value):
    adapter, hub, _broker, clock = _adapter()
    _untrusted(adapter, value)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "clock_unsynchronized"
    assert hub.calls == []


def test_a_clock_check_that_raises_is_untrusted():
    adapter, hub, _broker, clock = _adapter()

    def broken():
        raise OSError("stat failed")

    adapter.clock_trusted = broken
    _dark_for(adapter, clock, 300)
    assert adapter.status()["jtag_recovery"]["reset_blocked_reason"] == (
        "clock_unsynchronized"
    )
    with pytest.raises(IntegrationError):
        adapter.jtag_check()
    assert hub.calls == []


def test_kexec_is_refused_before_any_sync_and_starts_no_quiet_period():
    adapter, _hub, broker, _clock = _adapter(broker=Broker())
    _untrusted(adapter)
    with pytest.raises(IntegrationError) as error:
        adapter.reboot()
    assert error.value.code == "clock_unsynchronized"
    assert adapter._last_reboot is None
    assert broker.calls == []  # not even an identity probe


def test_kexec_stays_available_offline_after_an_earlier_sync():
    clocks = Clocks()
    clocks.sync()
    adapter, _hub, _broker, _clock = _adapter(broker=Broker())
    adapter.clock_trusted = clocks.trust()
    clocks.advance(6 * 3600)  # Internet lost hours ago; no new sync
    assert adapter.reboot()["outcome"] == "restart_requested"


# ----- end to end through the job store ----------------------------------------------


def _store(tmp_path, clocks):
    from butters.actions.store import ActionStateStore

    return ActionStateStore(
        tmp_path / "actions.sqlite3",
        load_assistant_settings().actions,
        clock=lambda: clocks.wall,
    )


def _record(store, skill, state="completed"):
    with store._lock, store._connect() as connection:
        connection.execute(
            "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
            "created_at,state,stage) VALUES (?,?,?,?,?,?,?,?,?)",
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
            ),
        )


def _process(store, clocks):
    """A Butters process on this store, as the web service wires it."""

    adapter, hub, broker, clock = _adapter(broker=Broker())
    adapter.clock = lambda: clocks.wall
    adapter.clock_trusted = clocks.trust()
    broker.ok, broker.error_code = False, "host_unreachable"
    adapter.attach_jtag(
        hub,
        persisted_kexec_at=lambda: store.last_job_that_may_have_run(
            "reboot_kr260", not_run=Kr260Adapter.RESTART_NOT_SENT
        ),
        persisted_reset_at=lambda: store.last_job_that_may_have_run(
            "reset_kr260_jtag", not_run=kr260_jtag.RESET_NOT_SENT
        ),
    )
    return adapter, hub, broker, clock


def test_no_recovery_job_can_be_stamped_before_a_large_forward_correction(tmp_path):
    # Cold boot a day behind with no NTP: nothing that starts a wait can run,
    # so no job is stamped with the wrong clock.
    clocks = Clocks(behind=86_400)
    store = _store(tmp_path, clocks)
    adapter, hub, broker, clock = _process(store, clocks)
    with pytest.raises(IntegrationError) as error:
        adapter.reboot()
    assert error.value.code == "clock_unsynchronized"
    broker.ok, broker.error_code = False, "host_unreachable"
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "clock_unsynchronized"
    assert hub.calls == []
    assert store.last_job_that_may_have_run("reboot_kr260", not_run=frozenset()) is None


def test_a_restart_after_the_correction_keeps_both_waits(tmp_path):
    # Synced boot: a kexec request and a reset are stored with the right
    # clock. Butters restarts; the new process still honours both waits.
    clocks = Clocks(behind=86_400)
    clocks.advance(40)
    clocks.sync()
    store = _store(tmp_path, clocks)
    _record(store, "reboot_kr260")
    clocks.advance(120)
    _record(store, "reset_kr260_jtag")
    clocks.advance(60)
    adapter, hub, _broker, clock = _process(store, clocks)
    _dark_for(adapter, clock, 300)
    clocks.advance(300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_check()
    assert error.value.code == "restart_recent"
    clocks.advance(kr260_jtag.KEXEC_QUIET_SECONDS)
    adapter.status(refresh=True)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []


def test_a_backward_clock_never_ends_a_wait_early(tmp_path):
    clocks = Clocks()
    clocks.sync()
    store = _store(tmp_path, clocks)
    _record(store, "reset_kr260_jtag")
    clocks.advance(60)
    adapter, hub, _broker, clock = _process(store, clocks)
    _dark_for(adapter, clock, 300)
    # The clock is stepped back an hour: untrusted, so nothing runs.
    clocks.step(-3600)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "clock_unsynchronized"
    # A later sync restores the clock; the stored reset still rate-limits.
    clocks.sync()
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "reset_rate_limited"
    assert hub.calls == []


def test_a_forward_step_never_ends_a_wait_early(tmp_path):
    clocks = Clocks()
    clocks.sync()
    store = _store(tmp_path, clocks)
    _record(store, "reset_kr260_jtag")
    clocks.advance(60)
    adapter, hub, _broker, clock = _process(store, clocks)
    _dark_for(adapter, clock, 300)
    # A day forward would make the stored reset look a day old.
    clocks.step(86_400)
    for run in (adapter.jtag_reset, adapter.jtag_check):
        with pytest.raises(IntegrationError) as error:
            run()
        assert error.value.code == "clock_unsynchronized"
    assert hub.calls == []


def test_corrupted_job_history_with_a_trusted_clock_still_refuses(tmp_path):
    clocks = Clocks()
    clocks.sync()
    store = _store(tmp_path, clocks)
    adapter, hub, _broker, clock = _process(store, clocks)
    database = tmp_path / "actions.sqlite3"
    for sidecar in (
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        sidecar.unlink(missing_ok=True)
    database.write_bytes(b"not a database, " * 512)
    _dark_for(adapter, clock, 300)
    with pytest.raises(IntegrationError) as error:
        adapter.jtag_reset()
    assert error.value.code == "restart_recent"
    assert hub.calls == []


def test_the_service_uses_the_real_clock_check(tmp_path):
    from test_kr260_admin_api import _application

    _app, service, _nas = _application(tmp_path)
    trust = service.assistant.kr260_adapter.clock_trusted
    assert isinstance(trust, ClockTrust)
    assert trust() is False  # conftest: no marker under test


def test_the_admin_page_explains_the_clock_block():
    script = (
        Path(__file__).resolve().parents[1] / "src/butters/web/static/assets/admin.js"
    )
    assert "clock_unsynchronized:" in script.read_text()


def test_kexec_settings_untouched():
    reboot = load_assistant_settings().actions.kr260_reboot
    assert (reboot.enabled, reboot.configured) == (False, False)
    assert replace(reboot) == reboot


# ----- authentication and the Admin endpoint ------------------------------------------


async def _exercise_clock_block(tmp_path):
    import httpx
    from test_kr260_admin_api import (
        ADMIN,
        _elevate,
        _jtag_application,
        _passkey,
        _session,
    )
    from test_nas_admin_controls import _await_job

    app, service, hub = _jtag_application(tmp_path)
    adapter = service.assistant.kr260_adapter
    adapter.clock_trusted = lambda: False
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    seen = {"hub": hub}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            status = await http.get("/api/admin/tools/kr260?refresh=1", headers=ADMIN)
            seen["status_code"] = status.status_code
            seen["before"] = status.json()["jtag_recovery"]
            mutation = await _session(http, ADMIN)
            await _elevate(http, mutation)

            async def reset():
                first = await http.post(
                    "/api/admin/tools/kr260/jtag",
                    headers=mutation,
                    json={"operation": "reset", "confirm": True},
                )
                body = first.json()
                verified = await _passkey(
                    http, mutation, body["pending_action"]["pending_action_id"]
                )
                job = await _await_job(
                    http, mutation, verified.json()["jobs"][0]["job_id"]
                )
                return body, job

            seen["first"], seen["blocked_job"] = await reset()
            seen["calls_while_blocked"] = len(hub.calls)
            adapter.clock_trusted = lambda: True
            _first, seen["later_job"] = await reset()
    finally:
        await app.state.shutdown_workers()
        del service
    return seen


def test_a_fresh_passkey_cannot_override_an_untrusted_clock(tmp_path):
    import asyncio

    seen = asyncio.run(_exercise_clock_block(tmp_path))
    assert seen["status_code"] == 200
    assert seen["before"]["reset_blocked_reason"] == "clock_unsynchronized"
    assert seen["before"]["reset_permitted"] is False
    # Still authenticated as before: fresh passkey, no agent call before it.
    assert seen["first"]["authentication_required"] == "fresh"
    assert seen["blocked_job"]["state"] == "failed"
    assert seen["blocked_job"]["failure_code"] == "clock_unsynchronized"
    assert seen["calls_while_blocked"] == 0
    # The refusal sent nothing, so it starts no interval: once the clock is
    # trusted, a new authorization resets exactly once.
    assert seen["later_job"]["state"] == "completed", seen["later_job"]
    assert len(seen["hub"].calls) == 1
