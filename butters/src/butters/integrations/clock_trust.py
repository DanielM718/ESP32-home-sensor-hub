"""Whether this Pi's wall clock can be trusted to order KR260 recovery events.

The Pi 4 has no RTC. At boot systemd-timesyncd restores the time it saved at
the last sync or shutdown, which lags by however long the Pi was off, and it
steps the clock forward on first NTP contact. A recovery job stamped before
that step would later look older than it is, and the persisted waits in
kr260_jtag (10 minutes after a kexec request, 30 between resets) would end
early. So Butters starts or times a KR260 recovery operation only while:

* timesyncd has synchronized during this boot. Its marker lives on tmpfs and
  is touched at every sync, so it cannot predate the boot.
* The marker is consistent: not before this boot, and not in the future.
* The wall clock has not stepped against the monotonic clock since the marker
  last changed. NTP slewing moves both together; a step, such as a manual
  `date -s`, does not. A new sync, which touches the marker, re-anchors.

Synchronized earlier this boot and offline now is trusted: the clock keeps
running from the sync, with drift of seconds per day. Every failure to read
or interpret this state is untrusted. Nothing here changes the clock.

With a ``baseline`` record (the web service passes one in its state
directory), the anchor also survives a Butters restart. It stores the boot ID,
boot time and wall time of the last trusted anchor:

* A new process on the same boot checks the clock against it, so a step made
  while Butters was down, or just before a restart, is still caught. With no
  sync since the baseline, wall and boot time cannot legitimately diverge
  (NTP slewing moves both), so the tolerance is STEP_SECONDS however long
  ago that was. After syncs it missed, NTP may have stepped the clock by
  accumulated drift: 100 ppm of the elapsed time, capped at
  RESTART_SYNC_ALLOWANCE_SECONDS.
* A sync this process sees live is trusted and becomes the new baseline:
  timesyncd has just set the time.
* A new boot must not have a wall clock earlier than the stored one.
* A missing or unreadable baseline cannot rule out an earlier step, so the
  clock stays untrusted until timesyncd's next sync.
* A detected step is stored too, so it stays latched across a restart.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from butters.integrations.safety_state import SafetyRecord, finite_time

MARKER = Path("/run/systemd/timesync/synchronized")
# Slack for comparing the marker with the boot and with now.
MARKER_TOLERANCE_SECONDS = 5.0
# Largest wall-versus-monotonic difference that is not a step.
STEP_SECONDS = 2.0
# Allowed drift per elapsed second when comparing with a stored baseline,
# across NTP corrections made while Butters was down (100 ppm), capped.
DRIFT_PER_SECOND = 1e-4
RESTART_SYNC_ALLOWANCE_SECONDS = 30.0
# The largest clock error this module can fail to detect. Persisted waits are
# lengthened by it, so no undetected error can end one early.
MAX_UNDETECTED_SECONDS = RESTART_SYNC_ALLOWANCE_SECONDS + STEP_SECONDS
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def _boot_id() -> str:
    value = BOOT_ID.read_text(encoding="ascii").strip()
    if not value:
        raise ValueError("empty boot id")
    return value


class ClockTrust:
    def __init__(
        self,
        *,
        marker: Path | None = None,
        stat: Callable[[Path], os.stat_result] = os.stat,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        boottime: Callable[[], float] = _boottime,
        baseline: SafetyRecord | None = None,
        boot_id: Callable[[], str] = _boot_id,
    ) -> None:
        # Read at construction, so tests can point the default elsewhere.
        self.marker = MARKER if marker is None else marker
        self._stat = stat
        self._wall = wall
        self._monotonic = monotonic
        self._boottime = boottime
        self.baseline = baseline
        self._boot_id = boot_id
        # (marker mtime, wall, monotonic) when the marker was last seen change.
        self._anchor: tuple[float, float, float] | None = None
        self._stepped = False
        # Status views and requests call this from different threads.
        self._lock = threading.Lock()

    def __call__(self) -> bool:
        return self.trusted()

    def trusted(self) -> bool:
        with self._lock:
            return self._trusted()

    def _trusted(self) -> bool:
        try:
            synced_at = float(self._stat(self.marker).st_mtime)
            wall = float(self._wall())
            mono = float(self._monotonic())
            booted_for = float(self._boottime())
        except Exception:  # noqa: BLE001 - unreadable state is untrusted.
            return False
        boot_started = wall - booted_for
        if not (
            boot_started - MARKER_TOLERANCE_SECONDS
            <= synced_at
            <= wall + MARKER_TOLERANCE_SECONDS
        ):
            # Not from this boot, or ahead of the clock: the clock or the
            # marker has moved since. Latched like a step, so putting the
            # clock back by hand does not restore trust; the next sync does.
            if self._anchor is not None and not self._stepped:
                self._stepped = True
                self._remember_step()
            return False
        anchor = self._anchor
        if anchor is None or anchor[0] != synced_at:
            first_look = anchor is None
            self._anchor = (synced_at, wall, mono)
            self._stepped = False
            if self.baseline is not None:
                self._stepped = not self._baseline_allows(
                    synced_at, wall, booted_for, first_look=first_look
                )
            return not self._stepped
        skew = (wall - anchor[1]) - (mono - anchor[2])
        if abs(skew) > STEP_SECONDS and not self._stepped:
            # Latched until the next sync re-anchors, also across a restart.
            self._stepped = True
            self._remember_step()
        return not self._stepped

    def _baseline_allows(
        self, synced_at: float, wall: float, booted_for: float, *, first_look: bool
    ) -> bool:
        """Check a new anchor against the stored one, and store it if trusted."""

        assert self.baseline is not None
        try:
            boot = self._boot_id()
        except Exception:  # noqa: BLE001 - an unknown boot is untrusted.
            return False
        if not first_look:
            # A sync seen live: timesyncd has just set the time.
            return self._store(boot, synced_at, wall, booted_for)
        stored = self.baseline.read()
        if not isinstance(stored, dict):
            # Missing or corrupt: a step before this process started cannot be
            # ruled out. Only a sync seen by this process re-establishes trust.
            return False
        stored_wall = finite_time(stored.get("wall"))
        stored_boottime = finite_time(stored.get("boottime"))
        stored_marker = finite_time(stored.get("marker"))
        if stored_wall is None or stored_boottime is None or stored_marker is None:
            return False
        if stored.get("boot_id") == boot:
            if stored.get("stepped") is True and stored_marker == synced_at:
                return False
            elapsed = booted_for - stored_boottime
            if elapsed < -STEP_SECONDS:
                return False
            expected = stored_wall + elapsed
            allowed = (
                STEP_SECONDS
                if stored_marker == synced_at
                else min(
                    STEP_SECONDS + DRIFT_PER_SECOND * max(0.0, elapsed),
                    RESTART_SYNC_ALLOWANCE_SECONDS,
                )
            )
            if abs(wall - expected) > allowed:
                return False
            if stored_marker == synced_at:
                return True  # a restart: keep the original reference
        elif wall < stored_wall - MARKER_TOLERANCE_SECONDS:
            # A new boot whose synchronized clock is earlier than a trusted
            # time already seen: one of the two is wrong.
            return False
        return self._store(boot, synced_at, wall, booted_for)

    def _store(
        self, boot: str, synced_at: float, wall: float, booted_for: float
    ) -> bool:
        assert self.baseline is not None
        try:
            self.baseline.write(
                {
                    "boot_id": boot,
                    "marker": synced_at,
                    "wall": wall,
                    "boottime": booted_for,
                    "stepped": False,
                }
            )
        except OSError:
            # Unprotected across a restart: do not trust.
            return False
        return True

    def _remember_step(self) -> None:
        if self.baseline is None:
            return
        try:
            self.baseline.update(
                lambda stored: (
                    {**stored, "stepped": True} if isinstance(stored, dict) else None
                )
            )
        except OSError:
            pass  # still latched in this process
