"""KR260 JTAG POR recovery: the guarded XSDB script, its wrapper, and the verdict."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from butters.integrations.kr260_reset import (
    Heartbeat,
    classify,
    main,
    parse_console,
    parse_heartbeats,
    parse_pings,
)

BUTTERS = Path(__file__).resolve().parents[1]
TCL = BUTTERS / "config" / "kr260" / "kr260-jtag-por.tcl"
PS1 = BUTTERS / "config" / "kr260" / "kr260-jtag-por.ps1"
HARNESS = BUTTERS / "tests" / "tcl" / "kr260_jtag_por_harness.tcl"
BEFORE = "63d18cfd-0a69-48ae-a4f5-74dfffa10eae"
AFTER = "bdaa97be-9c3f-4789-bb06-c7f22d24749d"
T0 = 1_793_240_000.0


def _code(path: Path) -> str:
    marker = "#" if path.suffix in {".tcl", ".ps1"} else "//"
    return "\n".join(line.split(marker, 1)[0] for line in path.read_text().splitlines())


# --- the XSDB script -------------------------------------------------------


def test_tcl_issues_only_rst_por_and_no_debug_or_programming_command() -> None:
    code = _code(TCL)
    assert re.findall(r"\brst\b[^\n]*", code) == ["rst -por} err]} {"]
    for forbidden in (
        r"rst\s+-(system|srst|processor|cores|cluster|dap|ps)\b",
        r"\b(stop|con|mwr|rwr|dow|fpga|bpadd|stp|nxt)\b",
        r"\bexec\b",
    ):
        assert not re.search(forbidden, code), forbidden


def test_tcl_pins_the_kr260_cable_and_chain() -> None:
    text = TCL.read_text()
    assert "variable product SCK-KR" in text
    assert "variable devices {xck26 04724093 arm_dap 5ba00477}" in text
    assert 'ne "confirm-por"' in text
    assert "finally" in text and "catch {disconnect}" in text


@pytest.mark.skipif(
    not (os.environ.get("KR260_TCLSH") or shutil.which("tclsh")),
    reason="no plain tclsh here (the harness ran on the desktop's Git tclsh 8.6.17)",
)
def test_tcl_harness_passes() -> None:
    tclsh = os.environ.get("KR260_TCLSH") or shutil.which("tclsh")
    result = subprocess.run(
        [tclsh, str(HARNESS), str(TCL)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY failures=0" in result.stdout


def test_harness_refuses_an_interpreter_with_xsdb_commands() -> None:
    text = HARNESS.read_text()
    assert "this is not a plain tclsh" in text
    for command in ("connect", "jtag", "targets", "rst", "disconnect"):
        assert command in text.split("foreach command {", 1)[1].split("}", 1)[0]


# --- the Windows wrapper ---------------------------------------------------


def test_wrapper_refuses_while_any_debugger_runs_before_starting_its_own() -> None:
    code = _code(PS1)
    refuse = code.index("REFUSED_DEBUGGER_ACTIVE")
    start = code.index("Start-Process -FilePath $Xsdb")
    stop = code.index("Stop-Process -Id $leftover.Id")
    assert refuse < start < stop
    assert "'hw_server', 'xsdb', 'rdi_xsdb', 'cs_server', 'vivado'" in code


def test_wrapper_passes_only_fixed_arguments_and_never_resets_itself() -> None:
    code = _code(PS1)
    assert "[ValidateSet('check', 'por')]" in code
    assert "[ValidatePattern('^[A-Z0-9]{12}A$')]" in code
    assert not re.search(r"\brst\b|Invoke-Expression|iex\b", code)
    assert re.search(r"exit 0 }\s*\nexit 1", code)


# --- the verdict -----------------------------------------------------------

# Real line shapes from this board's COM5 logger and the Pi heartbeat.
POR_CONSOLE = """\
2026-10-29T00:15:28.213Z  Zynq MP First Stage Boot Loader
2026-10-29T00:15:28.306Z Reset Mode\t:\tSystem Reset
2026-10-29T00:15:42.913Z Starting kernel ...
2026-10-29T00:16:37.540Z kria login:
"""
WARM_CONSOLE = """\
2026-10-29T00:04:59.808Z [ 3814.786005] reboot: Restarting system
"""
KEXEC_CONSOLE = """\
2026-10-29T01:55:32.815Z [    0.000000] Booting Linux on physical CPU 0x0000000000 [0x410fd034]
2026-10-29T01:56:27.344Z kria login:
"""


def _iso(offset: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(T0 + offset, UTC).isoformat().replace("+00:00", "Z")


def _verdict(*, console=(), beats=(), pings=(), now=300.0):
    return classify(
        issued_at=T0,
        now=T0 + now,
        boot_id_before=BEFORE,
        console=[(T0 + t, line) for t, line in console],
        heartbeats=[Heartbeat(T0 + t, b) for t, b in beats],
        pings=[(T0 + t, ok) for t, ok in pings],
    )


def test_a_cold_boot_banner_and_a_new_boot_confirm_the_por() -> None:
    v = _verdict(
        console=[(0.9, "Zynq MP First Stage Boot Loader"), (15, "Starting kernel ...")],
        beats=[(-5, BEFORE[:8]), (20, None), (75, AFTER[:8])],
        pings=[(1, False), (70, True)],
    )
    assert v.state == "por_confirmed"
    assert v.fsbl_after_s == 0.9 and v.ssh_after_s == 75
    assert not v.needs_power_cycle


def test_a_returned_command_without_a_banner_is_no_effect() -> None:
    v = _verdict(
        beats=[(5, BEFORE[:8]), (20, BEFORE[:8]), (40, BEFORE)],
        pings=[(t, True) for t in range(0, 60, 5)],
        now=60,
    )
    assert v.state == "no_effect"
    assert not v.needs_power_cycle


def test_no_banner_and_no_answer_needs_a_power_cycle() -> None:
    v = _verdict(
        beats=[(10, None), (20, None)], pings=[(16, False), (30, False)], now=40
    )
    assert v.state == "dark_without_console"
    assert v.needs_power_cycle


def test_the_warm_reset_message_is_never_a_por() -> None:
    v = _verdict(console=[(2, "[ 3814.78] reboot: Restarting system")], now=10)
    assert v.state == "warm_path" and v.needs_power_cycle


def test_a_new_boot_without_a_banner_is_not_a_por() -> None:
    v = _verdict(console=[(0.2, "[    0.000000] Booting Linux")], beats=[(60, AFTER)])
    assert v.state == "unexpected_restart"


@pytest.mark.parametrize(
    ("console", "now", "reason"),
    (
        ([(1, "Zynq MP First Stage Boot Loader")], 62, "no kernel"),
        (
            [(1, "Zynq MP First Stage Boot Loader"), (15, "Starting kernel ...")],
            152,
            "no login",
        ),
        (
            [
                (1, "Zynq MP First Stage Boot Loader"),
                (15, "Starting kernel ..."),
                (70, "kria login:"),
            ],
            241,
            "SSH never",
        ),
    ),
)
def test_a_reset_stuck_in_boot_needs_a_power_cycle(console, now, reason) -> None:
    v = _verdict(console=console, beats=[(30, None)], now=now)
    assert v.state == "stuck_in_boot" and reason in v.reason
    assert v.needs_power_cycle


def test_early_evidence_is_pending_or_booting() -> None:
    assert _verdict(now=5).state == "pending"
    assert (
        _verdict(console=[(1, "Zynq MP First Stage Boot Loader")], now=30).state
        == "booting"
    )


def test_console_lines_before_the_reset_do_not_count() -> None:
    v = _verdict(
        console=[
            (-30, "Zynq MP First Stage Boot Loader"),
            (-29, "reboot: Restarting system"),
        ],
        beats=[(20, BEFORE[:8])],
        pings=[(20, True)],
        now=30,
    )
    assert v.state == "no_effect"


def test_real_log_shapes_parse() -> None:
    console = parse_console(
        POR_CONSOLE + "2026-10-29T00:15:00.000Z ### port open (#1)\n"
    )
    assert (
        next(line.strip() for _, line in console) == "Zynq MP First Stage Boot Loader"
    )
    assert all("###" not in line for _, line in console)
    beats = parse_heartbeats(
        "2026-10-29T01:56:33.723Z rc=0 ms=6652 bdaa97be online=0-3 timer=1,2,3,4 sleep0=1,\n"
        "2026-10-29T01:56:17.056Z rc=255 ms=807 ssh: connect to host 192.168.1.125 port 22:"
        " No route to host\n"
    )
    assert [b.boot_id for b in beats] == ["bdaa97be", None]
    pings = parse_pings(
        "[1793238732.354348] 64 bytes from 192.168.1.125: icmp_seq=1 ttl=64 time=0.163 ms\n"
        "[1793232333.889439] From 192.168.1.157 icmp_seq=58 Destination Host Unreachable\n"
    )
    assert pings == [(1793238732.354348, True), (1793232333.889439, False)]


def test_this_sessions_real_events_classify_as_they_happened() -> None:
    # 2026-10-29: warm reboot (wedge), Admin kexec (new boot, no banner), and the
    # 12 V power cycle (banner + new boot, the same evidence a POR must produce).
    warm = parse_console(WARM_CONSOLE)
    v = classify(
        issued_at=warm[0][0] - 8,
        now=warm[0][0] + 60,
        boot_id_before=BEFORE,
        console=warm,
        heartbeats=[],
        pings=[],
    )
    assert v.state == "warm_path"

    kexec = parse_console(KEXEC_CONSOLE)
    v = classify(
        issued_at=kexec[0][0] - 6,
        now=kexec[-1][0] + 10,
        boot_id_before=BEFORE,
        console=kexec,
        heartbeats=[Heartbeat(kexec[-1][0] + 6, AFTER[:8])],
        pings=[],
    )
    assert v.state == "unexpected_restart"

    cold = parse_console(POR_CONSOLE)
    v = classify(
        issued_at=cold[0][0] - 1,
        now=cold[-1][0] + 10,
        boot_id_before=BEFORE,
        console=cold,
        heartbeats=[Heartbeat(cold[-1][0] + 6, "208bb0b2")],
        pings=[],
    )
    assert v.state == "por_confirmed"
    assert v.fsbl_after_s == 1.0


def test_cli_reads_the_monitoring_logs(tmp_path, capsys) -> None:
    (tmp_path / "console.log").write_text(
        f"{_iso(0.8)} Zynq MP First Stage Boot Loader\n{_iso(15)} Starting kernel ...\n"
    )
    (tmp_path / "hb.txt").write_text(f"{_iso(80)} rc=0 ms=600 {AFTER[:8]} online=0-3\n")
    (tmp_path / "ping.txt").write_text(
        f"[{T0 + 75:.6f}] 64 bytes from 192.168.1.125: x\n"
    )

    code = main(
        [
            "--issued-at",
            _iso(0),
            "--boot-id-before",
            BEFORE,
            "--console",
            str(tmp_path / "console.log"),
            "--heartbeat",
            str(tmp_path / "hb.txt"),
            "--ping",
            str(tmp_path / "ping.txt"),
            "--now",
            _iso(90),
        ]
    )

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["state"] == "por_confirmed" and out["needs_power_cycle"] is False
