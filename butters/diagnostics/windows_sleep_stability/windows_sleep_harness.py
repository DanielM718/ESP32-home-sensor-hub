#!/usr/bin/env python3
"""Bounded, fixed-target S3 stability harness for DESKTOP-G4CFVL1.

This is engineering instrumentation, not a production action surface.  The Windows
host, account, SSH identity, MAC, broadcast, PowerShell scripts, sleep operation,
probe cadence, recovery limits, and cycle profiles are constants.  Runtime options
select only a reviewed experiment profile and a sanitized local configuration label.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TARGET_HOSTNAME = "DESKTOP-G4CFVL1"
TARGET_IP = "192.168.1.209"
TARGET_USER = "Daniel"
TARGET_MAC = "34:5A:60:D7:4C:2C"
TARGET_BROADCAST = "192.168.1.255"
TARGET_NIC = "Realtek PCIe 5GbE Family Controller"

SSH_BINARY = "/usr/bin/ssh"
PING_BINARY = "/usr/bin/ping"
WOL_BINARY = "/usr/bin/wakeonlan"
SSH_IDENTITY = "/home/dmejiame/.ssh/id_ed25519"

WINDOWS_CONTROL = "C:/ProgramData/Butters/desktop-control.ps1"
WINDOWS_EVIDENCE = "C:/ProgramData/Butters/sleep-diagnostic-collect-cycle.ps1"

PROBE_INTERVAL_SECONDS = 12.0
OFFLINE_PROBE_INTERVAL_SECONDS = 2.0
RECOVERY_PROBE_INTERVAL_SECONDS = 2.0
SSH_PROBE_INTERVAL_SECONDS = 3.0
OFFLINE_TIMEOUT_SECONDS = 90.0
NETWORK_RECOVERY_TIMEOUT_SECONDS = 120.0
SSH_RECOVERY_TIMEOUT_SECONDS = 120.0
PHYSICAL_RECOVERY_TIMEOUT_SECONDS = 300.0
INTERCYCLE_SETTLE_SECONDS = 20.0
LOWER_LEVEL_CLOCK_TOLERANCE_SECONDS = 1.0
TRANSIENT_MAINTENANCE_TIMEOUT_SECONDS = 600.0
TRANSIENT_MAINTENANCE_POLL_SECONDS = 30.0
SILENT_NETWORK_GUARD_SECONDS = 30.0

SCRIPT_DIR = Path(__file__).resolve().parent
EVIDENCE_DIR = SCRIPT_DIR / "evidence"
CYCLE_DIR = EVIDENCE_DIR / "cycles"
JSONL_LOG = EVIDENCE_DIR / "experiment-log.jsonl"
CSV_LOG = EVIDENCE_DIR / "experiment-log.csv"
ENGINEERING_LOG = EVIDENCE_DIR / "engineering-log.md"

MODE_PROFILES: dict[str, tuple[int, ...]] = {
    "baseline": (300, 300, 300, 300, 300),
    "verification": (300, 300, 300),
    "qualification": (300, 300, 300, 300, 300, 300, 300, 300, 900, 900),
    "no_wol_isolation": (300,),
    "no_wake_sources_isolation": (300,),
    "silent_network_isolation": (300,),
}

PHYSICAL_RECOVERY_MODES = {
    "no_wol_isolation",
    "no_wake_sources_isolation",
    "silent_network_isolation",
}
NIC_UNARMED_ISOLATION_MODES = {"no_wol_isolation", "silent_network_isolation"}

SSH_BASE = (
    SSH_BINARY,
    "-i",
    SSH_IDENTITY,
    "-o",
    "BatchMode=yes",
    "-o",
    "IdentitiesOnly=yes",
    "-o",
    "ConnectTimeout=6",
    "-o",
    "ConnectionAttempts=1",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ClearAllForwardings=yes",
    "-T",
    f"{TARGET_USER}@{TARGET_IP}",
)
HOSTNAME_COMMAND = "cmd.exe /d /c hostname"
BEFORE_COMMAND = (
    "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
    f"-File {WINDOWS_EVIDENCE} -Phase Before"
)
AFTER_COMMAND = (
    "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
    f"-File {WINDOWS_EVIDENCE} -Phase After"
)
SLEEP_COMMAND = (
    "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
    f"-File {WINDOWS_CONTROL} -Operation Sleep"
)

CSV_FIELDS = (
    "cycle_id",
    "experiment_stage",
    "configuration_change",
    "sleep_request_time",
    "last_observed_online_time",
    "confirmed_offline_time",
    "quiet_window_seconds",
    "quiet_window_observed_seconds",
    "spontaneous_resume",
    "spontaneous_resume_time",
    "wol_sent",
    "wol_count",
    "wol_time",
    "network_return_time",
    "ssh_return_time",
    "network_latency",
    "ssh_latency",
    "resume_initiation_time",
    "windows_resume_time",
    "wake_source",
    "entered_s3",
    "pure_s3",
    "hybrid_sleep_observed",
    "rebooted_instead",
    "nic_wake_armed",
    "rt640x64_error",
    "ndis_error",
    "usb_error",
    "device_error",
    "scheduled_task_correlation",
    "wol_causal_evidence",
    "stop_reason",
    "notes",
    "clean_cycle",
)

CONFIG_LABEL = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")


class InterruptedCycle(RuntimeError):
    """Raised when SIGINT/SIGTERM requests a fail-closed stop."""


stop_requested = False


def request_stop(_signum: int, _frame: object) -> None:
    global stop_requested
    stop_requested = True


def require_running() -> None:
    if stop_requested:
        raise InterruptedCycle("local harness interrupted; no further wake or sleep action issued")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        match = re.match(r"^(.*\.\d{6})\d*([+-]\d\d:\d\d)$", normalized)
        if not match:
            return None
        parsed = datetime.fromisoformat(match.group(1) + match.group(2))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    left = parse_timestamp(start)
    right = parse_timestamp(end)
    if left is None or right is None:
        return None
    return round((right - left).total_seconds(), 3)


def run_capture(argv: tuple[str, ...], timeout: float) -> subprocess.CompletedProcess[str]:
    require_running()
    return subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ssh(command: str, timeout: float = 25.0) -> subprocess.CompletedProcess[str]:
    return run_capture((*SSH_BASE, command), timeout)


def parse_json_output(output: str) -> dict[str, Any]:
    for line in reversed(output.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("fixed Windows collector did not return a JSON object")


def hostname_healthy() -> bool:
    try:
        result = ssh(HOSTNAME_COMMAND, timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().upper() == TARGET_HOSTNAME


def ping_reachable() -> bool:
    require_running()
    try:
        result = subprocess.run(
            (PING_BINARY, "-n", "-c", "1", "-W", "1", TARGET_IP),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def sleep_until(deadline: float) -> None:
    while True:
        require_running()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30.0))


def collect_before() -> dict[str, Any]:
    result = ssh(BEFORE_COMMAND, timeout=30.0)
    if result.returncode != 0:
        raise RuntimeError(f"before collector failed: {result.stderr.strip()[:500]}")
    return parse_json_output(result.stdout)


def collect_after() -> dict[str, Any]:
    # A real cycle produced a 63-second display-driver resume while networking
    # and authenticated SSH were already available.  Give Windows time to finish
    # logging that failure rather than killing the evidence collector mid-resume.
    result = ssh(AFTER_COMMAND, timeout=120.0)
    if result.returncode != 0:
        raise RuntimeError(f"after collector failed: {result.stderr.strip()[:500]}")
    return parse_json_output(result.stdout)


def request_sleep() -> subprocess.CompletedProcess[str]:
    return ssh(SLEEP_COMMAND, timeout=25.0)


def send_one_wol() -> subprocess.CompletedProcess[str]:
    return run_capture(
        (WOL_BINARY, "-i", TARGET_BROADCAST, TARGET_MAC),
        timeout=10.0,
    )


def output_lines(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    raw = value.get("output", [])
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw] if isinstance(raw, list) else []


def fixed_adapter_valid(evidence: dict[str, Any]) -> bool:
    adapter = evidence.get("fixed_adapter")
    if not isinstance(adapter, dict):
        return False
    return bool(
        adapter.get("interface_description") == TARGET_NIC
        and adapter.get("expected_mac_match") is True
        and adapter.get("status") == "Up"
        and adapter.get("pnp_status") == "OK"
        and adapter.get("pnp_problem") in (0, None)
    )


def nic_wake_armed(evidence: dict[str, Any]) -> bool:
    return TARGET_NIC in output_lines(evidence.get("wake_armed"))


def armed_wake_devices(evidence: dict[str, Any]) -> list[str]:
    return [
        line.strip()
        for line in output_lines(evidence.get("wake_armed"))
        if line.strip() and line.strip().upper() != "NONE"
    ]


def transient_maintenance_request_active(evidence: dict[str, Any]) -> bool:
    combined = "\n".join(output_lines(evidence.get("power_requests"))).lower()
    return "defragsvc" in combined or "drive optimizer is running" in combined


def correlated_tasks(evidence: dict[str, Any], resume_time: str | None) -> str:
    state = str(evidence.get("task_scheduler_log_state") or "unknown")
    if state != "enabled":
        return f"unavailable_log_{state}"
    resume = parse_timestamp(resume_time)
    if resume is None:
        return "none_resume_time_unavailable"
    matches: list[str] = []
    events = evidence.get("task_scheduler_events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            stamp = parse_timestamp(str(event.get("time_created_utc") or ""))
            if stamp is None or abs((stamp - resume).total_seconds()) > 60:
                continue
            message = " ".join(str(event.get("message") or "").split())
            matches.append(
                f"{event.get('event_id')}:{message[:180]}" if message else str(event.get("event_id"))
            )
    return " | ".join(matches) if matches else "none"


def make_cycle(mode: str, index: int, dwell: int, configuration: str) -> dict[str, Any]:
    started = utc_now()
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "schema_version": 1,
        "cycle_id": f"{mode}-{run_stamp}-{index:02d}",
        "cycle_index": index,
        "experiment_stage": mode,
        "configuration_change": configuration,
        "cycle_started_time": started,
        "sleep_request_time": None,
        "sleep_request_ack_time": None,
        "last_observed_online_time": None,
        "confirmed_offline_time": None,
        "quiet_window_seconds": dwell,
        "quiet_window_observed_seconds": 0.0,
        "post_sleep_network_silence_seconds": 0.0,
        "spontaneous_resume": False,
        "spontaneous_resume_time": None,
        "wol_sent": False,
        "wol_count": 0,
        "wol_time": None,
        "physical_wake_request_time": None,
        "network_return_time": None,
        "ssh_return_time": None,
        "network_latency": None,
        "ssh_latency": None,
        "resume_initiation_time": None,
        "windows_resume_time": None,
        "wake_source": None,
        "entered_s3": False,
        "pure_s3": False,
        "hybrid_sleep_observed": False,
        "rebooted_instead": False,
        "nic_wake_armed": False,
        "rt640x64_error": False,
        "ndis_error": False,
        "usb_error": False,
        "device_error": False,
        "scheduled_task_correlation": None,
        "wol_causal_evidence": "unavailable",
        "network_healthy": False,
        "ssh_healthy": False,
        "stop_reason": None,
        "notes": "",
        "clean_cycle": False,
        "before_evidence": None,
        "after_evidence": None,
    }


def mark_stop(cycle: dict[str, Any], reason: str, note: str | None = None) -> None:
    if not cycle.get("stop_reason"):
        cycle["stop_reason"] = reason
    if note:
        existing = str(cycle.get("notes") or "")
        cycle["notes"] = (existing + "; " + note).strip("; ")


def wait_for_offline(cycle: dict[str, Any]) -> bool:
    deadline = time.monotonic() + OFFLINE_TIMEOUT_SECONDS
    consecutive_failures = 0
    while time.monotonic() < deadline:
        require_running()
        observed = utc_now()
        if ping_reachable():
            cycle["last_observed_online_time"] = observed
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures >= 2:
                cycle["confirmed_offline_time"] = observed
                return True
        sleep_until(min(deadline, time.monotonic() + OFFLINE_PROBE_INTERVAL_SECONDS))
    return False


def observe_quiet_window(cycle: dict[str, Any]) -> bool:
    start = time.monotonic()
    deadline = start + float(cycle["quiet_window_seconds"])
    next_probe = start + PROBE_INTERVAL_SECONDS
    while next_probe < deadline:
        sleep_until(next_probe)
        if ping_reachable():
            stamp = utc_now()
            cycle["spontaneous_resume"] = True
            cycle["spontaneous_resume_time"] = stamp
            cycle["network_return_time"] = stamp
            cycle["quiet_window_observed_seconds"] = round(time.monotonic() - start, 3)
            return False
        next_probe += PROBE_INTERVAL_SECONDS
    sleep_until(deadline)
    cycle["quiet_window_observed_seconds"] = round(time.monotonic() - start, 3)
    # This final pre-WOL probe ensures the full interval elapsed continuously.
    if ping_reachable():
        stamp = utc_now()
        cycle["spontaneous_resume"] = True
        cycle["spontaneous_resume_time"] = stamp
        cycle["network_return_time"] = stamp
        return False
    return True


def wait_for_network(cycle: dict[str, Any]) -> bool:
    deadline = time.monotonic() + NETWORK_RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        require_running()
        if ping_reachable():
            cycle["network_return_time"] = utc_now()
            return True
        sleep_until(min(deadline, time.monotonic() + RECOVERY_PROBE_INTERVAL_SECONDS))
    return False


def wait_for_physical_recovery(cycle: dict[str, Any]) -> bool:
    deadline = time.monotonic() + PHYSICAL_RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        require_running()
        if ping_reachable():
            cycle["network_return_time"] = utc_now()
            return True
        sleep_until(min(deadline, time.monotonic() + RECOVERY_PROBE_INTERVAL_SECONDS))
    return False


def wait_for_ssh(cycle: dict[str, Any]) -> bool:
    deadline = time.monotonic() + SSH_RECOVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        require_running()
        if hostname_healthy():
            cycle["ssh_return_time"] = utc_now()
            return True
        sleep_until(min(deadline, time.monotonic() + SSH_PROBE_INTERVAL_SECONDS))
    return False


def apply_after_evidence(cycle: dict[str, Any], evidence: dict[str, Any]) -> None:
    cycle["after_evidence"] = evidence
    cycle["resume_initiation_time"] = evidence.get("resume_initiation_time_utc")
    cycle["windows_resume_time"] = evidence.get("windows_resume_time_utc")
    cycle["wake_source"] = evidence.get("wake_source")
    cycle["entered_s3"] = evidence.get("entered_s3") is True
    cycle["hybrid_sleep_observed"] = evidence.get("hybrid_sleep_observed") is True
    cycle["pure_s3"] = bool(cycle["entered_s3"] and not cycle["hybrid_sleep_observed"])
    cycle["rebooted_instead"] = evidence.get("rebooted_instead") is True
    cycle["nic_wake_armed"] = nic_wake_armed(evidence)
    cycle["rt640x64_error"] = bool(evidence.get("realtek_errors"))
    cycle["ndis_error"] = bool(evidence.get("ndis_errors"))
    cycle["usb_error"] = bool(evidence.get("usb_errors"))
    cycle["device_error"] = bool(evidence.get("device_errors"))
    cycle["scheduled_task_correlation"] = correlated_tasks(
        evidence, str(cycle.get("resume_initiation_time") or "")
    )
    cycle["network_healthy"] = fixed_adapter_valid(evidence)
    cycle["ssh_healthy"] = bool(cycle.get("ssh_return_time"))

    resume = parse_timestamp(str(cycle.get("resume_initiation_time") or ""))
    wol = parse_timestamp(str(cycle.get("wol_time") or ""))
    if resume and wol:
        delta = (resume - wol).total_seconds()
        if delta < -LOWER_LEVEL_CLOCK_TOLERANCE_SECONDS:
            cycle["spontaneous_resume"] = True
            if not cycle.get("spontaneous_resume_time"):
                cycle["spontaneous_resume_time"] = cycle["resume_initiation_time"]
            cycle["wol_causal_evidence"] = "resume_began_before_wol"
            mark_stop(cycle, "spontaneous_non_wol_resume", "lower-level Windows resume preceded WOL")
        elif delta >= -LOWER_LEVEL_CLOCK_TOLERANCE_SECONDS:
            cycle["wol_causal_evidence"] = "resume_began_after_wol"
    elif resume and not wol:
        physical_request = parse_timestamp(
            str(cycle.get("physical_wake_request_time") or "")
        )
        if (
            cycle.get("experiment_stage") in PHYSICAL_RECOVERY_MODES
            and physical_request
            and (resume - physical_request).total_seconds()
            >= -LOWER_LEVEL_CLOCK_TOLERANCE_SECONDS
        ):
            cycle["wol_causal_evidence"] = "resume_began_after_physical_wake_request"
        else:
            cycle["spontaneous_resume"] = True
            if not cycle.get("spontaneous_resume_time"):
                cycle["spontaneous_resume_time"] = cycle["resume_initiation_time"]
            cycle["wol_causal_evidence"] = "no_wol_sent"
    elif wol:
        cycle["wol_causal_evidence"] = "resume_timestamp_unavailable"

    if cycle.get("wol_time"):
        cycle["network_latency"] = elapsed_seconds(
            str(cycle.get("wol_time")), str(cycle.get("network_return_time") or "")
        )
        cycle["ssh_latency"] = elapsed_seconds(
            str(cycle.get("wol_time")), str(cycle.get("ssh_return_time") or "")
        )


def assess_cycle(cycle: dict[str, Any]) -> None:
    if cycle.get("stop_reason"):
        return
    # TargetState=4/WakeFromState=4 plus firmware S3 events is the physical S3
    # criterion.  The pre-existing hybrid setting is recorded independently; it
    # is not silently treated as a fix and can be isolated later as its own value.
    failures: list[tuple[bool, str]] = [
        (not cycle.get("confirmed_offline_time"), "sleep_entry_or_offline_confirmation_failed"),
        (cycle.get("spontaneous_resume") is True, "spontaneous_non_wol_resume"),
        (cycle.get("wol_count") != 1, "exactly_one_wol_requirement_failed"),
        (not cycle.get("network_return_time"), "network_recovery_failed"),
        (not cycle.get("ssh_return_time"), "ssh_recovery_failed"),
        (cycle.get("entered_s3") is not True, "non_s3_transition"),
        (cycle.get("rebooted_instead") is True, "unexpected_reboot"),
        (cycle.get("nic_wake_armed") is not True, "nic_not_wake_armed"),
        (cycle.get("rt640x64_error") is True, "realtek_hardware_io_error"),
        (cycle.get("ndis_error") is True, "ndis_resume_error"),
        (cycle.get("usb_error") is True, "usb_resume_error"),
        (cycle.get("device_error") is True, "device_resume_error"),
        (cycle.get("network_healthy") is not True, "post_resume_network_unhealthy"),
        (cycle.get("ssh_healthy") is not True, "post_resume_ssh_unhealthy"),
        (
            float(cycle.get("quiet_window_observed_seconds") or 0)
            < float(cycle.get("quiet_window_seconds") or 0),
            "quiet_window_incomplete",
        ),
    ]
    for failed, reason in failures:
        if failed:
            mark_stop(cycle, reason)
            return
    cycle["clean_cycle"] = True


def append_logs(cycle: dict[str, Any]) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    CYCLE_DIR.mkdir(parents=True, exist_ok=True)
    cycle_path = CYCLE_DIR / f"{cycle['cycle_id']}.json"
    cycle_path.write_text(json.dumps(cycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with JSONL_LOG.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({key: cycle.get(key) for key in cycle if key not in {"before_evidence", "after_evidence"}}, sort_keys=True) + "\n")

    new_csv = not CSV_LOG.exists() or CSV_LOG.stat().st_size == 0
    with CSV_LOG.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new_csv:
            writer.writeheader()
        writer.writerow(cycle)

    if not ENGINEERING_LOG.exists():
        ENGINEERING_LOG.write_text("# Windows sleep stability engineering log\n\n", encoding="utf-8")
    result = "CLEAN" if cycle.get("clean_cycle") else f"FAILED ({cycle.get('stop_reason')})"
    conclusion = (
        "cycle met every diagnostic criterion"
        if cycle.get("clean_cycle")
        else "automatic cycling stopped for evidence analysis"
    )
    with ENGINEERING_LOG.open("a", encoding="utf-8") as stream:
        stream.write(
            f"- {utc_now()} | {cycle['cycle_id']} | hypothesis/config: "
            f"`{cycle['configuration_change']}` | result: {result} | conclusion: {conclusion}.\n"
        )


def run_cycle(mode: str, index: int, dwell: int, configuration: str) -> dict[str, Any]:
    cycle = make_cycle(mode, index, dwell, configuration)
    print(f"[{utc_now()}] {cycle['cycle_id']} preflight", flush=True)
    try:
        require_running()
        if not hostname_healthy():
            mark_stop(cycle, "preflight_ssh_or_hostname_failed")
            return cycle

        before = collect_before()
        cycle["before_evidence"] = before
        if before.get("hostname") != TARGET_HOSTNAME:
            mark_stop(cycle, "preflight_hostname_mismatch")
            return cycle
        if not fixed_adapter_valid(before):
            mark_stop(cycle, "preflight_fixed_nic_unhealthy")
            return cycle
        if mode not in PHYSICAL_RECOVERY_MODES and not nic_wake_armed(before):
            mark_stop(cycle, "preflight_nic_not_wake_armed")
            return cycle
        if mode in NIC_UNARMED_ISOLATION_MODES and nic_wake_armed(before):
            mark_stop(cycle, "preflight_nic_still_wake_armed_for_no_wol_isolation")
            return cycle
        if mode == "no_wake_sources_isolation" and armed_wake_devices(before):
            mark_stop(cycle, "preflight_wake_armed_devices_remain_for_no_source_isolation")
            return cycle

        maintenance_deadline = time.monotonic() + TRANSIENT_MAINTENANCE_TIMEOUT_SECONDS
        while mode != "baseline" and transient_maintenance_request_active(before):
            print(
                f"[{utc_now()}] transient Drive Optimizer request active; no sleep requested",
                flush=True,
            )
            if time.monotonic() >= maintenance_deadline:
                mark_stop(cycle, "preflight_transient_maintenance_request_active")
                return cycle
            sleep_until(
                min(
                    maintenance_deadline,
                    time.monotonic() + TRANSIENT_MAINTENANCE_POLL_SECONDS,
                )
            )
            if not hostname_healthy():
                mark_stop(cycle, "preflight_ssh_or_hostname_failed")
                return cycle
            before = collect_before()
            cycle["before_evidence"] = before
            if mode == "no_wake_sources_isolation":
                wake_state_valid = not armed_wake_devices(before)
            elif mode in NIC_UNARMED_ISOLATION_MODES:
                wake_state_valid = not nic_wake_armed(before)
            else:
                wake_state_valid = nic_wake_armed(before)
            if not fixed_adapter_valid(before) or not wake_state_valid:
                mark_stop(cycle, "preflight_fixed_nic_or_wake_state_changed")
                return cycle
        if mode == "baseline" and transient_maintenance_request_active(before):
            cycle["notes"] = (
                "Drive Optimizer request active at baseline sleep request; recorded as confound"
            )

        cycle["last_observed_online_time"] = utc_now()
        cycle["sleep_request_time"] = utc_now()
        print(f"[{cycle['sleep_request_time']}] requesting fixed S3 operation once", flush=True)
        sleep_result = request_sleep()
        cycle["sleep_request_ack_time"] = utc_now()
        if sleep_result.returncode != 0:
            mark_stop(cycle, "sleep_request_failed", sleep_result.stderr.strip()[:500])
            return cycle

        if mode == "silent_network_isolation":
            silence_started = time.monotonic()
            silence_seconds = float(dwell) + SILENT_NETWORK_GUARD_SECONDS
            print(
                f"[{cycle['sleep_request_ack_time']}] no Pi-to-target traffic for "
                f"{silence_seconds:.0f}s; offline state will be verified retrospectively",
                flush=True,
            )
            sleep_until(silence_started + silence_seconds)
            cycle["post_sleep_network_silence_seconds"] = round(
                time.monotonic() - silence_started, 3
            )
            if ping_reachable():
                stamp = utc_now()
                cycle["spontaneous_resume"] = True
                cycle["spontaneous_resume_time"] = stamp
                cycle["network_return_time"] = stamp
                if not wait_for_ssh(cycle):
                    mark_stop(cycle, "silent_isolation_ssh_recovery_failed")
                    return cycle
                evidence = collect_after()
                apply_after_evidence(cycle, evidence)
                if cycle.get("entered_s3"):
                    cycle["confirmed_offline_time"] = evidence.get("windows_sleep_time_utc")
                    sleep_time = str(evidence.get("windows_sleep_time_utc") or "")
                    resume_time = str(evidence.get("resume_initiation_time_utc") or "")
                    observed = elapsed_seconds(sleep_time, resume_time)
                    if observed is not None:
                        cycle["quiet_window_observed_seconds"] = observed
                    mark_stop(
                        cycle,
                        "spontaneous_non_wol_resume",
                        "genuine S3 confirmed retrospectively; resume preceded the first post-sleep network probe",
                    )
                else:
                    mark_stop(cycle, "silent_isolation_non_s3_transition")
                return cycle

            cycle["confirmed_offline_time"] = utc_now()
            cycle["quiet_window_observed_seconds"] = float(dwell)
            cycle["physical_wake_request_time"] = utc_now()
            print(
                f"[{cycle['physical_wake_request_time']}] target still unreachable after silent "
                "dwell; press the physical keyboard or power button once",
                flush=True,
            )
            if not wait_for_physical_recovery(cycle):
                mark_stop(cycle, "physical_recovery_timeout_no_wol_sent")
                return cycle
            if not wait_for_ssh(cycle):
                mark_stop(cycle, "ssh_recovery_failed_after_physical_wake")
                return cycle
            evidence = collect_after()
            apply_after_evidence(cycle, evidence)
            if cycle.get("spontaneous_resume"):
                mark_stop(cycle, "resume_preceded_physical_wake_request")
            else:
                mark_stop(
                    cycle,
                    "silent_network_isolation_completed",
                    "desktop remained unreachable through a traffic-free five-minute dwell",
                )
            return cycle

        if not wait_for_offline(cycle):
            mark_stop(cycle, "sleep_entry_or_offline_confirmation_failed")
            return cycle

        print(
            f"[{cycle['confirmed_offline_time']}] offline; local {dwell}s no-WOL dwell started",
            flush=True,
        )
        stayed_asleep = observe_quiet_window(cycle)
        if not stayed_asleep:
            print(
                f"[{cycle['spontaneous_resume_time']}] spontaneous reachability; WOL suppressed",
                flush=True,
            )
            if not wait_for_ssh(cycle):
                mark_stop(cycle, "spontaneous_resume_ssh_recovery_failed")
                return cycle
            evidence = collect_after()
            apply_after_evidence(cycle, evidence)
            mark_stop(cycle, "spontaneous_non_wol_resume")
            return cycle

        if mode in PHYSICAL_RECOVERY_MODES:
            cycle["physical_wake_request_time"] = utc_now()
            print(
                f"[{cycle['physical_wake_request_time']}] no-WOL isolation dwell complete; "
                "WOL suppressed; press the physical keyboard or power button once",
                flush=True,
            )
            if not wait_for_physical_recovery(cycle):
                mark_stop(cycle, "physical_recovery_timeout_no_wol_sent")
                return cycle
            if not wait_for_ssh(cycle):
                mark_stop(cycle, "ssh_recovery_failed_after_physical_wake")
                return cycle
            evidence = collect_after()
            apply_after_evidence(cycle, evidence)
            if cycle.get("spontaneous_resume"):
                mark_stop(
                    cycle,
                    "resume_preceded_physical_wake_request",
                    "lower-level resume timestamp preceded requested physical recovery",
                )
            else:
                mark_stop(
                    cycle,
                    "no_wol_isolation_completed",
                    "desktop remained unreachable for full dwell and resumed after physical recovery request",
                )
            return cycle

        require_running()
        cycle["wol_time"] = utc_now()
        cycle["wol_sent"] = True
        cycle["wol_count"] = 1
        print(f"[{cycle['wol_time']}] dwell complete; sending exactly one fixed WOL", flush=True)
        wol_result = send_one_wol()
        if wol_result.returncode != 0:
            mark_stop(cycle, "wol_send_failed", wol_result.stderr.strip()[:500])
            return cycle

        if not wait_for_network(cycle):
            mark_stop(cycle, "network_recovery_failed_after_one_wol")
            return cycle
        if not wait_for_ssh(cycle):
            mark_stop(cycle, "ssh_recovery_failed_after_one_wol")
            return cycle

        evidence = collect_after()
        apply_after_evidence(cycle, evidence)
        assess_cycle(cycle)
        return cycle
    except InterruptedCycle as exc:
        mark_stop(cycle, "local_harness_interrupted", str(exc))
        return cycle
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        mark_stop(cycle, "diagnostic_harness_error", str(exc)[:500])
        return cycle


def validate_environment() -> None:
    for path in (SSH_BINARY, PING_BINARY, WOL_BINARY, SSH_IDENTITY):
        if not Path(path).exists():
            raise RuntimeError(f"required fixed dependency is missing: {path}")
    if TARGET_IP != "192.168.1.209" or TARGET_MAC != "34:5A:60:D7:4C:2C":
        raise RuntimeError("fixed target constants changed unexpectedly")
    print(
        json.dumps(
            {
                "target_hostname": TARGET_HOSTNAME,
                "target_ip": TARGET_IP,
                "target_mac": TARGET_MAC,
                "target_broadcast": TARGET_BROADCAST,
                "target_nic": TARGET_NIC,
                "profiles": MODE_PROFILES,
                "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
                "wol_limit_per_cycle": 1,
            },
            indent=2,
        )
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=tuple(MODE_PROFILES))
    parser.add_argument(
        "--configuration-label",
        default="current_configuration",
        help="sanitized local experiment metadata only; never reaches Windows",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="check fixed constants/dependencies without probing or changing the desktop",
    )
    args = parser.parse_args(argv)
    if not CONFIG_LABEL.fullmatch(args.configuration_label):
        parser.error("configuration label must match [a-z0-9][a-z0-9_-]{0,63}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    validate_environment()
    if args.validate_only:
        return 0

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    profile = MODE_PROFILES[args.mode]
    clean_count = 0
    for index, dwell in enumerate(profile, start=1):
        cycle = run_cycle(args.mode, index, dwell, args.configuration_label)
        append_logs(cycle)
        print(
            json.dumps(
                {
                    "cycle_id": cycle["cycle_id"],
                    "clean_cycle": cycle["clean_cycle"],
                    "stop_reason": cycle["stop_reason"],
                    "spontaneous_resume": cycle["spontaneous_resume"],
                    "wol_count": cycle["wol_count"],
                    "network_latency": cycle["network_latency"],
                    "ssh_latency": cycle["ssh_latency"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if not cycle["clean_cycle"]:
            print(f"[{utc_now()}] fail-closed stop; analyze before another cycle", flush=True)
            return 1
        clean_count += 1
        if index < len(profile):
            sleep_until(time.monotonic() + INTERCYCLE_SETTLE_SECONDS)

    if args.mode == "qualification" and clean_count != 10:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
