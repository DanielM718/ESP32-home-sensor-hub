"""The KR260 kexec restart script, run against fake kexec, systemctl, dumpimage and sysfs."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "config" / "kr260" / "kr260-kexec-restart.sh"
)
RELEASE = "6.8.0-1015-xilinx"
CMDLINE = " root=LABEL=writable rootwait console=ttyPS1,115200 cma=800M \n"

FAKE_KEXEC = r"""#!/bin/bash
echo "kexec $*" >> "$FAKE_LOG"
case "$FAKE_MODE:$1" in
  loadfail:-l) exit 1 ;;
  *:-l) echo 1 > "$KX_LOADED" ;;
  *:-u) echo 0 > "$KX_LOADED" ;;
esac
"""

FAKE_SYSTEMCTL = r"""#!/bin/bash
echo "systemctl $*" >> "$FAKE_LOG"
if [ "$1" = show ]; then echo "$FAKE_KEXEC_WD"; exit 0; fi
[ "$FAKE_MODE" = systemctlfail ] && exit 1
exit 0
"""


class Board:
    def __init__(self, tmp_path: Path, *, mode="ok", kexec_wd="0", watchdog="inactive"):
        self.tmp = tmp_path
        self.boot = tmp_path / "boot"
        self.boot.mkdir()
        self.kernel = self.boot / f"vmlinuz-{RELEASE}"
        self.initrd = self.boot / f"initrd.img-{RELEASE}"
        self.kernel.write_bytes(b"\x1f\x8b fake gzip Image")
        self.initrd.write_bytes(b"070701 fake cpio")
        self.loaded = tmp_path / "kexec_loaded"
        self.loaded.write_text("0\n")
        self.fdt = tmp_path / "fdt"
        self.fdt.write_bytes(b"\xd0\x0d\xfe\xed")
        self.cmdline = tmp_path / "cmdline"
        self.cmdline.write_text(CMDLINE)
        self.watchdogs = tmp_path / "watchdog"
        for name, state in (("watchdog0", watchdog), ("watchdog1", "inactive")):
            (self.watchdogs / name).mkdir(parents=True)
            (self.watchdogs / name / "state").write_text(f"{state}\n")
        self.log = tmp_path / "calls.log"
        self.mode, self.kexec_wd = mode, kexec_wd
        for name, body in (("kexec", FAKE_KEXEC), ("systemctl", FAKE_SYSTEMCTL)):
            (tmp_path / name).write_text(body)
            (tmp_path / name).chmod(0o755)
        k = hashlib.sha1(self.kernel.read_bytes()).hexdigest()
        i = hashlib.sha1(self.initrd.read_bytes()).hexdigest()
        self.dumpimage = tmp_path / "dumpimage"
        self.dumpimage.write_text(
            "#!/bin/sh\ncat <<'EOF'\n"
            f" Image 0 (kernel-1)\n  Hash value:   {k}\n"
            f" Image 1 (ramdisk-1)\n  Hash value:   {i}\nEOF\n"
        )
        self.dumpimage.chmod(0o755)

    def run(self, *args: str):
        env = {
            "PATH": os.environ["PATH"],
            "KX_KEXEC": str(self.tmp / "kexec"),
            "KX_SYSTEMCTL": str(self.tmp / "systemctl"),
            "KX_DUMPIMAGE": str(self.dumpimage),
            "KX_LOADED": str(self.loaded),
            "KX_WATCHDOGS": str(self.watchdogs),
            "KX_BOOT": str(self.boot),
            "KX_FIT": str(self.tmp / "image.fit"),
            "KX_FDT": str(self.fdt),
            "KX_CMDLINE": str(self.cmdline),
            "KX_RELEASE": RELEASE,
            "FAKE_LOG": str(self.log),
            "FAKE_MODE": self.mode,
            "FAKE_KEXEC_WD": self.kexec_wd,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def staged(self) -> str:
        return self.loaded.read_text().strip()


def assert_no_reboot_path(board: Board) -> None:
    for call in board.calls():
        assert not re.search(
            r"\b(reboot|poweroff|halt|shutdown|default|rescue|emergency)\b", call
        ), call
        if call.startswith("kexec "):
            assert call.split()[1] in {"-l", "-u"}, call
        else:
            assert call in {
                "systemctl show -p KExecWatchdogUSec --value",
                "systemctl --no-wall --no-block kexec",
            }, call


def test_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_script_text_has_no_reboot_or_direct_execute() -> None:
    code = "\n".join(line.split("#", 1)[0] for line in SCRIPT.read_text().splitlines())
    for forbidden in (
        r"\breboot\b",
        r"\bshutdown\b",
        r"\bpoweroff\b",
        r"\bhalt\b",
        r"\bcoldreboot\b",
        r"kexec\s+-e",
        r"--exec\b",
        r"--force\b",
    ):
        assert not re.search(forbidden, code), forbidden


def test_success_stages_then_hands_off_to_clean_systemd_kexec(tmp_path) -> None:
    board = Board(tmp_path)

    result = board.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert board.calls() == [
        "systemctl show -p KExecWatchdogUSec --value",
        f"kexec -l {board.kernel} --initrd={board.initrd} --dtb={board.fdt} --reuse-cmdline",
        "systemctl --no-wall --no-block kexec",
    ]
    assert board.staged() == "1"
    assert_no_reboot_path(board)


def test_an_active_hardware_watchdog_stops_everything(tmp_path) -> None:
    board = Board(tmp_path, watchdog="active")

    result = board.run()

    assert result.returncode == 14
    assert board.calls() == []
    assert board.staged() == "0"


@pytest.mark.parametrize("value", ("30s", "10min", "infinity", ""))
def test_a_kexec_watchdog_setting_other_than_zero_stops_everything(
    tmp_path, value
) -> None:
    board = Board(tmp_path, kexec_wd=value)

    result = board.run()

    assert result.returncode == 14
    assert board.calls() == ["systemctl show -p KExecWatchdogUSec --value"]
    assert board.staged() == "0"


def test_failed_staging_unloads_and_never_restarts(tmp_path) -> None:
    board = Board(tmp_path, mode="loadfail")

    result = board.run()

    assert result.returncode == 21
    assert "systemctl --no-wall --no-block kexec" not in board.calls()
    assert board.staged() == "0"
    assert_no_reboot_path(board)


def test_a_refused_systemd_kexec_unloads_and_does_not_fall_back(tmp_path) -> None:
    board = Board(tmp_path, mode="systemctlfail")

    result = board.run()

    assert result.returncode == 22
    assert board.calls()[-1] == "kexec -u"
    assert board.staged() == "0"
    assert_no_reboot_path(board)


def test_an_already_staged_image_is_left_alone(tmp_path) -> None:
    board = Board(tmp_path)
    board.loaded.write_text("1\n")

    assert board.run().returncode == 10
    assert board.calls() == []
    assert board.staged() == "1"


@pytest.mark.parametrize("which", ("kernel", "initrd"))
def test_artifacts_that_differ_from_the_booted_fit_are_refused(tmp_path, which) -> None:
    board = Board(tmp_path)
    getattr(board, which).write_bytes(b"a different build")

    assert board.run().returncode == 12
    assert board.calls() == []


def test_a_cmdline_without_the_ssd_root_is_refused(tmp_path) -> None:
    board = Board(tmp_path)
    board.cmdline.write_text(" root=/dev/mmcblk0p2\n")

    assert board.run().returncode == 13
    assert board.calls() == []


def test_arguments_are_refused(tmp_path) -> None:
    board = Board(tmp_path)

    for args in (("reboot",), ("-e",), ("--force",)):
        assert board.run(*args).returncode == 2
    assert board.calls() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="needs a non-root user")
def test_requires_root_without_test_fakes() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "run with sudo" in result.stderr
