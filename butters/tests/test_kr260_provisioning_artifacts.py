"""Board-side KR260 provisioning artifacts: the deep-idle unit and the sudoers rule."""

from __future__ import annotations

import configparser
import shutil
import subprocess
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "config" / "kr260"


def _unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string((CONFIG / "butters-kr260-deep-idle.service").read_text())
    return parser


def test_deep_idle_unit_runs_the_installed_script_once_and_rolls_back_on_stop() -> None:
    unit = _unit()

    assert unit["Service"]["Type"] == "oneshot"
    assert unit["Service"]["RemainAfterExit"] == "yes"
    assert (
        unit["Service"]["ExecStart"] == "/usr/local/sbin/butters-kr260-deep-idle apply"
    )
    assert (
        unit["Service"]["ExecStop"]
        == "/usr/local/sbin/butters-kr260-deep-idle rollback"
    )
    assert unit["Install"]["WantedBy"] == "multi-user.target"


def test_deep_idle_unit_fails_visibly_instead_of_skipping() -> None:
    unit = _unit()

    # A Condition* would turn an unexpected idle layout into a silent skip;
    # the script must run, refuse, and leave the unit failed.
    for section in unit.sections():
        assert not any(key.startswith(("Condition", "Assert")) for key in unit[section])
    # The script writes /sys and /run; these would make that impossible.
    for key in ("ProtectKernelTunables", "ReadOnlyPaths", "ProtectSystem"):
        if key == "ProtectSystem":
            assert unit["Service"].get(key) in {None, "yes", "full"}
        else:
            assert key not in unit["Service"]


def test_deep_idle_unit_targets_only_the_reviewed_script() -> None:
    script = CONFIG / "kr260-deep-idle.sh"
    text = script.read_text()

    assert "TARGET=cpu-sleep-0" in text
    assert "state0 is not WFI" in text


@pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo not installed")
def test_sudoers_rule_parses_with_visudo() -> None:
    result = subprocess.run(
        ["visudo", "-cf", str(CONFIG / "butters-kr260-reboot.sudoers")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
