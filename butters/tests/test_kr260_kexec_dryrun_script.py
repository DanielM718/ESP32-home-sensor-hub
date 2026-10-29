"""The KR260 kexec dry-run script, run against a fake kexec, dumpimage and sysfs."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "config" / "kr260" / "kr260-kexec-dryrun.sh"
)
RELEASE = "6.8.0-1015-xilinx"
CMDLINE = (
    " root=LABEL=writable rootwait earlycon console=ttyPS1,115200 console=tty1"
    " clk_ignore_unused cma=800M \n"
)

FAKE_KEXEC = r"""#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$FAKE_MODE:$1" in
  ok:-l|unloadfail:-l|unloadonce:-l) echo 1 > "$KX_LOADED" ;;
  loadfail:-l) exit 1 ;;
  loadsilent:-l) exit 0 ;;
  unloadfail:-u) exit 1 ;;
  unloadonce:-u) if [ -e "$FAKE_LOG.once" ]; then echo 0 > "$KX_LOADED"; else touch "$FAKE_LOG.once"; exit 1; fi ;;
  *:-u) echo 0 > "$KX_LOADED" ;;
esac
"""


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


class Board:
    def __init__(self, tmp_path: Path, *, staged="0", cmdline=CMDLINE, mode="ok"):
        self.tmp = tmp_path
        self.boot = tmp_path / "boot"
        self.boot.mkdir()
        self.kernel = self.boot / f"vmlinuz-{RELEASE}"
        self.initrd = self.boot / f"initrd.img-{RELEASE}"
        self.kernel.write_bytes(b"\x1f\x8b fake gzip Image")
        self.initrd.write_bytes(b"070701 fake cpio")
        self.loaded = tmp_path / "kexec_loaded"
        self.loaded.write_text(f"{staged}\n")
        self.fdt = tmp_path / "fdt"
        self.fdt.write_bytes(b"\xd0\x0d\xfe\xed")
        self.cmdline = tmp_path / "cmdline"
        self.cmdline.write_text(cmdline)
        self.log = tmp_path / "kexec.log"
        self.mode = mode
        self.kexec = tmp_path / "kexec"
        self.kexec.write_text(FAKE_KEXEC)
        self.kexec.chmod(0o755)
        self.fit_kernel = sha1(self.kernel)
        self.fit_ramdisk = sha1(self.initrd)
        self.dumpimage = tmp_path / "dumpimage"
        self.write_dumpimage()

    def write_dumpimage(self) -> None:
        self.dumpimage.write_text(
            "#!/bin/sh\ncat <<'EOF'\n"
            "FIT description: Simple image with single Linux kernel, ramdisk and FDT blob\n"
            " Image 0 (kernel-1)\n  Hash algo:    sha1\n"
            f"  Hash value:   {self.fit_kernel}\n"
            " Image 1 (ramdisk-1)\n  Hash algo:    sha1\n"
            f"  Hash value:   {self.fit_ramdisk}\n"
            " Image 2 (fdt-zynqmp-smk-k26-revA.dtb)\n  Hash algo:    sha1\n"
            "  Hash value:   2de7d4924f4b4139411360d5c41dda7ba6f945af\nEOF\n"
        )
        self.dumpimage.chmod(0o755)

    def run(self, *args: str):
        env = {
            "PATH": os.environ["PATH"],
            "KX_KEXEC": str(self.kexec),
            "KX_DUMPIMAGE": str(self.dumpimage),
            "KX_LOADED": str(self.loaded),
            "KX_BOOT": str(self.boot),
            "KX_FIT": str(self.tmp / "image.fit"),
            "KX_FDT": str(self.fdt),
            "KX_CMDLINE": str(self.cmdline),
            "KX_RELEASE": RELEASE,
            "FAKE_LOG": str(self.log),
            "FAKE_MODE": self.mode,
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


def assert_never_executes(board: Board) -> None:
    for call in board.calls():
        first = call.split()[0]
        assert first in {"-l", "-u"}, call
        assert not re.search(
            r"(^|\s)(-e|--exec|-f|--force|-p|--load-panic)(\s|$)", call
        ), call


def test_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_script_text_contains_no_execute_or_reboot_path() -> None:
    code = "\n".join(line.split("#", 1)[0] for line in SCRIPT.read_text().splitlines())
    for forbidden in (
        r"kexec\s+-e",
        r"--exec\b",
        r"\bsystemctl\b",
        r"\breboot\b",
        r"\bshutdown\b",
        r"\bpoweroff\b",
        r"\bhalt\b",
        r"\bcoldreboot\b",
        r"\binit\s+6\b",
    ):
        assert not re.search(forbidden, code), forbidden


def test_success_stages_the_booted_kernel_and_unloads_it(tmp_path) -> None:
    board = Board(tmp_path)

    result = board.run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN PASS" in result.stdout
    assert board.calls() == [
        f"-l {board.kernel} --initrd={board.initrd} --dtb={board.fdt} --reuse-cmdline",
        "-u",
    ]
    assert board.staged() == "0"
    assert_never_executes(board)


def test_an_already_staged_image_is_left_alone(tmp_path) -> None:
    board = Board(tmp_path, staged="1")

    result = board.run()

    assert result.returncode == 10
    assert "already staged" in result.stdout
    assert board.calls() == []
    assert board.staged() == "1"


@pytest.mark.parametrize("missing", ("kernel", "initrd", "fdt", "cmdline"))
def test_missing_artifacts_refuse_before_kexec(tmp_path, missing) -> None:
    board = Board(tmp_path)
    getattr(board, missing).unlink()

    result = board.run()

    assert result.returncode == 11
    assert board.calls() == []


@pytest.mark.parametrize("which", ("kernel", "initrd"))
def test_artifacts_that_differ_from_the_booted_fit_are_refused(tmp_path, which) -> None:
    board = Board(tmp_path)
    getattr(board, which).write_bytes(b"a different build")

    result = board.run()

    assert result.returncode == 12
    assert "!= booted FIT" in result.stdout
    assert board.calls() == []


def test_unknown_fit_hashes_are_refused(tmp_path) -> None:
    board = Board(tmp_path)
    board.fit_kernel = ""
    board.write_dumpimage()

    assert board.run().returncode == 12
    assert board.calls() == []


def test_a_cmdline_without_the_ssd_root_is_refused(tmp_path) -> None:
    board = Board(tmp_path, cmdline=" root=/dev/mmcblk0p2 rootwait\n")

    result = board.run()

    assert result.returncode == 13
    assert board.calls() == []


@pytest.mark.parametrize("mode", ("loadfail", "loadsilent"))
def test_failed_staging_still_unloads_and_reports(tmp_path, mode) -> None:
    board = Board(tmp_path, mode=mode)

    result = board.run()

    assert result.returncode == 21
    assert "FAIL: staging" in result.stdout
    assert board.calls()[-1] == "-u"
    assert board.staged() == "0"
    assert_never_executes(board)


def test_unload_is_retried_once(tmp_path) -> None:
    board = Board(tmp_path, mode="unloadonce")

    result = board.run()

    assert result.returncode == 0, result.stdout
    assert [c.split()[0] for c in board.calls()] == ["-l", "-u", "-u"]
    assert board.staged() == "0"


def test_a_stuck_staged_kernel_gives_cleanup_and_never_falls_back(tmp_path) -> None:
    board = Board(tmp_path, mode="unloadfail")

    result = board.run()

    assert result.returncode == 20
    assert "CLEANUP NEEDED" in result.stdout
    assert "do NOT execute the staged kernel" in result.stdout
    assert [c.split()[0] for c in board.calls()] == ["-l", "-u", "-u"]
    assert_never_executes(board)


def test_arguments_are_refused(tmp_path) -> None:
    board = Board(tmp_path)

    for args in (("-e",), ("--exec",), ("now",)):
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
