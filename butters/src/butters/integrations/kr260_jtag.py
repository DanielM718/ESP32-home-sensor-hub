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


# The Admin page's vocabulary. Every value Butters can report has text there
# (tests/test_kr260_jtag_admin_ui.py).
BOARD_STATES = frozenset(
    {
        "online",
        "degraded",
        "unreachable",
        "unreachable_confirmed",
        "monitoring_unavailable",
    }
)
ATTEMPT_STATES = frozenset(
    {
        "requested",
        "confirmed",
        "booted",
        "no_effect",
        "unconfirmed",
        "failed",
        "check_passed",
        "check_failed",
    }
)
EVIDENCE = frozenset(
    {
        "new_boot_id",
        "same_boot_id",
        "uptime_after_request",
        "uptime_before_request",
        "identity_verified_without_boot_id",
        "identity_mismatch",
        "monitoring_unavailable",
        "network_only",
        "no_answer",
        "not_sent",
        "none",
    }
)
BLOCKED_REASONS = frozenset(
    {
        "gate_closed",
        "agent_unconfigured",
        "agent_disconnected",
        "agent_awaiting_heartbeat",
        "agent_heartbeat_aging",
        "agent_heartbeat_stale",
        "agent_jtag_not_offered",
        "board_reachable",
        "offline_not_established",
        "restart_recent",
        "reset_rate_limited",
    }
)


def is_dark(observation: dict[str, object]) -> bool:
    return (
        observation.get("operational_state") == "offline"
        and observation.get("network_reachable") is False
        and observation.get("ssh_available") is False
        and observation.get("identity_verified") is False
        and observation.get("error") in DARK_ERRORS
    )


@dataclass(slots=True)
class Attempt:
    """One JTAG request Butters sent to the Desktop Agent."""

    operation: str  # "check" or "reset"
    at: float  # monotonic
    previous_boot_id: str | None
    may_have_reset: bool
    error: str | None
    jtag_result: str | None
    issued_at: str | None
    # Latched once boot evidence decides it: (state, evidence).
    final: tuple[str, str] | None = None


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


# Failure codes of a reset job that ran nothing on the board: Butters and agent
# refusals, and wrapper refusals before rst -por. Every other failed or
# interrupted reset job may have pulsed the POR line and starts the interval.
RESET_NOT_SENT = frozenset(
    {
        "capability_unavailable",
        "board_reachable",
        "offline_not_established",
        "restart_recent",
        "reset_rate_limited",
        "invalid_action",
        "invalid_parameter",
        "invalid_request_id",
        "duplicate_request",
        "agent_unavailable",
        "unsupported_action",
        "superseded_connection",
        "busy",
        "cancelled",
        "debugger_active",
        "integrity_failed",
        "rate_limited",
        "jtag_connect_failed",
        "jtag_refused_usage",
        "jtag_refused_unconfirmed",
        "jtag_refused_chain",
        "jtag_refused_target",
        "jtag_refused_repeat",
        "jtag_refused_debugger_active",
        "jtag_refused_missing_script",
        # The script's own probe found the board answering (or could not
        # tell) immediately before attaching or before rst -por.
        "jtag_refused_reachable",
        "jtag_refused_probe",
    }
)

# Observation errors that are Butters-side: nothing is known about the board.
_MONITORING_ERRORS = frozenset({"ssh_unavailable", "capability_disabled", "disabled"})


def board_state(observation: dict[str, object], dark: DarkRun, now: float) -> str:
    """One word for what Butters currently knows about the board.

    online                 identity-verified answer
    degraded               something answered (ping, SSH as another host,
                           an auth failure) but not the verified board
    unreachable            dark, but not yet for DARK_SECONDS over two probes
    unreachable_confirmed  dark for DARK_SECONDS over at least two probes
    monitoring_unavailable Butters cannot observe (broker down, disabled)
    """

    if observation.get("identity_verified"):
        return "online"
    if observation.get("error") in _MONITORING_ERRORS:
        return "monitoring_unavailable"
    if not is_dark(observation):
        return "degraded"
    if dark.count >= 2 and dark.seconds(now) >= DARK_SECONDS:
        return "unreachable_confirmed"
    return "unreachable"


def reset_state(
    observation: dict[str, object],
    *,
    age_seconds: float,
    previous_boot_id: str | None,
) -> tuple[str, str]:
    """(state, evidence) for one reset that may have run.

    States: ``confirmed`` (the identity-verified board answers with a boot ID
    different from the last one Butters saw), ``booted`` (no previous boot ID
    was known, but the verified board's uptime began after the request),
    ``no_effect`` (the verified board answers from the same boot), and
    ``requested`` until CONFIRM_SECONDS, then ``unconfirmed``. A wrapper
    RST_ISSUED alone is only ever ``requested``.
    """

    if observation.get("identity_verified"):
        boot_id = observation.get("boot_id")
        uptime = observation.get("uptime_seconds")
        if previous_boot_id is not None and isinstance(boot_id, str) and boot_id:
            if boot_id != previous_boot_id:
                return "confirmed", "new_boot_id"
            return "no_effect", "same_boot_id"
        if isinstance(uptime, (int, float)):
            if uptime <= age_seconds:
                return "booted", "uptime_after_request"
            return "no_effect", "uptime_before_request"
        evidence = "identity_verified_without_boot_id"
    elif observation.get("operational_state") == "identity_failed":
        # Some other host answers at the board's address: never confirmed.
        evidence = "identity_mismatch"
    elif observation.get("error") in _MONITORING_ERRORS:
        evidence = "monitoring_unavailable"
    elif observation.get("network_reachable"):
        # Booting, or Linux up without SSH: not a confirmation.
        evidence = "network_only"
    else:
        evidence = "no_answer"
    return ("requested" if age_seconds < CONFIRM_SECONDS else "unconfirmed"), evidence
