"""Classify what a KR260 hardware reset request actually did.

`rst -por` returning without an error proves only that XSDB accepted the
command. A real power-on reset re-runs the boot ROM, so the console shows a new
FSBL banner within about a second, and the board later answers with a new boot
ID. This module turns the independent evidence (COM5 console lines, SSH
heartbeat boot IDs, ping samples) into one verdict, and applies the manual
recovery thresholds from KR260_MANAGEMENT.md. It reads no devices and has no
side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

FSBL_BANNER = "Zynq MP First Stage Boot Loader"
KERNEL_START = "Starting kernel"
LOGIN_PROMPT = "login:"
# The PMUFW warm reset: the path that wedges this board. A POR never prints it.
WARM_RESET = "reboot: Restarting system"

FSBL_WITHIN_SECONDS = 15.0
KERNEL_WITHIN_SECONDS = 60.0
LOGIN_WITHIN_SECONDS = 150.0
SSH_WITHIN_SECONDS = 240.0


@dataclass(frozen=True, slots=True)
class Heartbeat:
    at: float
    boot_id: str | None  # None when the SSH probe failed


@dataclass(frozen=True, slots=True)
class Verdict:
    state: str
    reason: str
    fsbl_after_s: float | None = None
    ssh_after_s: float | None = None
    new_boot_id: str | None = None

    @property
    def needs_power_cycle(self) -> bool:
        return self.state in {"stuck_in_boot", "dark_without_console", "warm_path"}


def _first(console: list[tuple[float, str]], marker: str, after: float) -> float | None:
    return next((t for t, line in console if t >= after and marker in line), None)


def _same_boot(boot_id: str, before: str) -> bool:
    # The heartbeat may record only a prefix of the boot ID.
    shorter, longer = sorted(
        (boot_id.replace("-", ""), before.replace("-", "")), key=len
    )
    return bool(shorter) and longer.startswith(shorter)


def classify(
    *,
    issued_at: float,
    now: float,
    boot_id_before: str,
    console: list[tuple[float, str]],
    heartbeats: list[Heartbeat],
    pings: list[tuple[float, bool]],
) -> Verdict:
    """One verdict for a reset issued at ``issued_at`` (epoch seconds)."""

    elapsed = now - issued_at
    if _first(console, WARM_RESET, issued_at) is not None:
        return Verdict(
            "warm_path",
            "console shows the PMUFW warm reset, not a power-on reset",
        )

    fsbl = _first(console, FSBL_BANNER, issued_at)
    after = [beat for beat in heartbeats if beat.at >= issued_at]
    new_boot = next(
        (b for b in after if b.boot_id and not _same_boot(b.boot_id, boot_id_before)),
        None,
    )

    if fsbl is not None:
        fsbl_after = fsbl - issued_at
        if new_boot is not None:
            return Verdict(
                "por_confirmed",
                "new FSBL banner and a new boot ID",
                fsbl_after_s=round(fsbl_after, 3),
                ssh_after_s=round(new_boot.at - issued_at, 3),
                new_boot_id=new_boot.boot_id,
            )
        kernel = _first(console, KERNEL_START, fsbl)
        login = _first(console, LOGIN_PROMPT, fsbl)
        if kernel is None and now - fsbl > KERNEL_WITHIN_SECONDS:
            return Verdict(
                "stuck_in_boot",
                "FSBL ran but no kernel started",
                fsbl_after_s=fsbl_after,
            )
        if login is None and now - fsbl > LOGIN_WITHIN_SECONDS:
            return Verdict(
                "stuck_in_boot",
                "kernel started but no login prompt",
                fsbl_after_s=fsbl_after,
            )
        if elapsed > SSH_WITHIN_SECONDS:
            return Verdict(
                "stuck_in_boot",
                "booted but SSH never returned",
                fsbl_after_s=fsbl_after,
            )
        return Verdict("booting", "FSBL seen; waiting for SSH", fsbl_after_s=fsbl_after)

    if new_boot is not None:
        return Verdict(
            "unexpected_restart",
            "a new boot ID without a cold-boot banner: not a power-on reset",
            ssh_after_s=round(new_boot.at - issued_at, 3),
            new_boot_id=new_boot.boot_id,
        )
    if elapsed < FSBL_WITHIN_SECONDS:
        return Verdict("pending", "waiting for the FSBL banner")
    later = [ok for t, ok in pings if t >= issued_at + FSBL_WITHIN_SECONDS]
    same_boot_alive = any(
        b.boot_id and _same_boot(b.boot_id, boot_id_before) for b in after
    )
    if same_boot_alive and (not later or all(later)):
        return Verdict(
            "no_effect",
            "no FSBL banner and the same boot is still running: the POR line did nothing",
        )
    return Verdict(
        "dark_without_console",
        "no FSBL banner and the board stopped answering",
    )


_CONSOLE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) (.*)$")
_HEARTBEAT = re.compile(r"^(\S+Z) rc=(\d+) ms=\d+(?: ([0-9a-f-]{8,36}) online=)?")
_PING = re.compile(r"^\[(\d+\.\d+)\] (.*)$")


def _epoch(stamp: str) -> float:
    return (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    )


def parse_console(text: str) -> list[tuple[float, str]]:
    """The COM5 logger's UTC line-stamped log ("<ISO>Z <text>")."""

    lines = []
    for raw in text.splitlines():
        match = _CONSOLE.match(raw)
        if match and not match[2].startswith("###"):
            lines.append((_epoch(match[1]), match[2]))
    return lines


def parse_heartbeats(text: str) -> list[Heartbeat]:
    """jtag-heartbeat.sh lines ("<ISO>Z rc=<n> ms=<n> <bootid> online=...")."""

    beats = []
    for raw in text.splitlines():
        match = _HEARTBEAT.match(raw)
        if match:
            ok = match[2] == "0" and match[3] is not None
            beats.append(Heartbeat(_epoch(match[1]), match[3] if ok else None))
    return beats


def parse_pings(text: str) -> list[tuple[float, bool]]:
    """`ping -D` output: a reply line is up; an error or unreachable line is down."""

    samples = []
    for raw in text.splitlines():
        match = _PING.match(raw)
        if match:
            samples.append((float(match[1]), " bytes from " in match[2]))
    return samples


def main(argv: list[str] | None = None) -> int:
    """`kr260-reset-verdict`: classify a reset from the existing monitoring logs."""

    import argparse
    import json
    import time
    from dataclasses import asdict
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="kr260-reset-verdict")
    parser.add_argument(
        "--issued-at", required=True, help="UTC ISO time the reset was issued"
    )
    parser.add_argument("--boot-id-before", required=True)
    parser.add_argument("--console", type=Path, required=True, help="COM5 logger .log")
    parser.add_argument("--heartbeat", type=Path, required=True)
    parser.add_argument("--ping", type=Path, required=True, help="`ping -D` output")
    parser.add_argument("--now", help="UTC ISO time to judge at (default: now)")
    args = parser.parse_args(argv)

    verdict = classify(
        issued_at=_epoch(args.issued_at),
        now=_epoch(args.now) if args.now else time.time(),
        boot_id_before=args.boot_id_before,
        console=parse_console(args.console.read_text(errors="replace")),
        heartbeats=parse_heartbeats(args.heartbeat.read_text(errors="replace")),
        pings=parse_pings(args.ping.read_text(errors="replace")),
    )
    print(
        json.dumps({**asdict(verdict), "needs_power_cycle": verdict.needs_power_cycle})
    )
    return 0 if verdict.state in {"por_confirmed", "pending", "booting"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
