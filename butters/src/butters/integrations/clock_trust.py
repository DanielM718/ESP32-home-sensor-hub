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
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

MARKER = Path("/run/systemd/timesync/synchronized")
# Slack for comparing the marker with the boot and with now.
MARKER_TOLERANCE_SECONDS = 5.0
# Largest wall-versus-monotonic difference that is not a step.
STEP_SECONDS = 2.0


def _boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


class ClockTrust:
    def __init__(
        self,
        *,
        marker: Path | None = None,
        stat: Callable[[Path], os.stat_result] = os.stat,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        boottime: Callable[[], float] = _boottime,
    ) -> None:
        # Read at construction, so tests can point the default elsewhere.
        self.marker = MARKER if marker is None else marker
        self._stat = stat
        self._wall = wall
        self._monotonic = monotonic
        self._boottime = boottime
        # (marker mtime, wall, monotonic) when the marker was last seen change.
        self._anchor: tuple[float, float, float] | None = None
        self._stepped = False

    def __call__(self) -> bool:
        return self.trusted()

    def trusted(self) -> bool:
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
            if self._anchor is not None:
                self._stepped = True
            return False
        anchor = self._anchor
        if anchor is None or anchor[0] != synced_at:
            self._anchor = (synced_at, wall, mono)
            self._stepped = False
            return True
        skew = (wall - anchor[1]) - (mono - anchor[2])
        if abs(skew) > STEP_SECONDS:
            # Latched until the next sync re-anchors.
            self._stepped = True
        return not self._stepped
