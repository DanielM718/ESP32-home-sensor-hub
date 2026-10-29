"""When Butters may ask the Desktop Agent for a KR260 JTAG power-on reset.

A JTAG POR is abrupt (no unmount), and attaching a debugger to a running board
with deep idle enabled hangs it (KR260_MANAGEMENT.md). Butters therefore asks
only for a board its own observations show has been dark for a while, never
shortly after a kexec restart it requested (that board is just booting), and
at most once per half hour. The agent applies its own refusals as well,
including its own ping from Windows. This module decides; it does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

# Continuous dark time before a reset is considered. A kexec restart is back in
# about 70 s and a cold boot in about 80 s, so five minutes is not a boot.
DARK_SECONDS = 300.0
# A board restarted through kexec is left alone this long.
KEXEC_QUIET_SECONDS = 600.0
# One reset attempt per half hour; after that a human decides.
POR_INTERVAL_SECONDS = 1800.0
# The POR on 2026-10-29 had SSH back after 77 s. Unconfirmed after this.
CONFIRM_SECONDS = 300.0
# Broker failures that mean the board itself did not answer. A broker outage,
# an authentication failure or an identity mismatch is not a dark board.
DARK_ERRORS = frozenset({"host_unreachable", "ssh_timeout"})


def is_dark(observation: dict[str, object]) -> bool:
    return (
        observation.get("operational_state") == "offline"
        and observation.get("network_reachable") is False
        and observation.get("ssh_available") is False
        and observation.get("identity_verified") is False
        and observation.get("error") in DARK_ERRORS
    )


@dataclass(slots=True)
class DarkRun:
    """The current unbroken run of dark observations (monotonic seconds)."""

    since: float | None = None
    count: int = 0

    def record(self, observation: dict[str, object], at: float) -> None:
        if not is_dark(observation):
            self.since, self.count = None, 0
            return
        if self.since is None:
            self.since = at
        self.count += 1

    def seconds(self, now: float) -> float:
        return 0.0 if self.since is None else now - self.since


def refusal(
    *,
    now: float,
    dark: DarkRun,
    last_kexec_at: float | None,
    last_reset_at: float | None,
    reset: bool,
) -> str | None:
    """The fixed refusal code for a check (``reset=False``) or reset, or None."""

    if dark.since is None:
        return "board_reachable"
    # Two observations at least DARK_SECONDS apart: one failed probe is not an
    # outage, and nothing is inferred from a single sample.
    if dark.count < 2 or dark.seconds(now) < DARK_SECONDS:
        return "offline_not_established"
    if last_kexec_at is not None and now - last_kexec_at < KEXEC_QUIET_SECONDS:
        return "restart_recent"
    if (
        reset
        and last_reset_at is not None
        and now - last_reset_at < POR_INTERVAL_SECONDS
    ):
        return "reset_rate_limited"
    return None


def reset_state(
    observation: dict[str, object],
    *,
    age_seconds: float,
    previous_boot_id: str | None,
) -> tuple[bool | None, str]:
    """(booted after the reset, state) for one requested reset.

    Only an identity-verified answer counts. With a known previous boot ID, a
    different one confirms; without one (Butters restarted while the board was
    dark), an uptime shorter than the time since the request does.
    """

    booted: bool | None = None
    if observation.get("identity_verified"):
        boot_id = observation.get("boot_id")
        uptime = observation.get("uptime_seconds")
        if previous_boot_id is not None and isinstance(boot_id, str) and boot_id:
            booted = boot_id != previous_boot_id
        elif isinstance(uptime, (int, float)):
            booted = uptime <= age_seconds
    if booted:
        return True, "confirmed"
    return booted, "requested" if age_seconds < CONFIRM_SECONDS else "unconfirmed"
