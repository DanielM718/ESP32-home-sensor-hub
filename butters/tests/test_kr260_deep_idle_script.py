"""The KR260 deep-idle script, run against a fake sysfs tree shaped like the board's."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "config" / "kr260" / "kr260-deep-idle.sh"
NOT_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="permission checks need a non-root user"
)


def board(tmp_path: Path, *, cpus=4, state0="WFI", deep="cpu-sleep-0") -> Path:
    root = tmp_path / "cpu"
    for cpu in range(cpus):
        for index, name in ((0, state0), (1, deep)):
            state = root / f"cpu{cpu}" / "cpuidle" / f"state{index}"
            state.mkdir(parents=True)
            (state / "name").write_text(f"{name}\n")
            (state / "disable").write_text("0\n")
    (root / "online").write_text(f"0-{cpus - 1}\n")
    return root


def run(root: Path, mode: str, *, orig: Path | None = None):
    env = {
        "PATH": os.environ["PATH"],
        "KR260_IDLE_CPU_ROOT": str(root),
        "KR260_IDLE_ORIG": str(orig or root.parent / "orig"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), mode],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def disables(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text().strip()
        for path in sorted(root.glob("cpu*/cpuidle/state*/disable"))
    }


def test_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_check_validates_the_board_layout_without_writing(tmp_path) -> None:
    root = board(tmp_path)
    before = disables(root)

    result = run(root, "check")

    assert result.returncode == 0, result.stderr
    assert "CHECK OK" in result.stdout
    assert disables(root) == before
    assert not (tmp_path / "orig").exists()


def test_apply_disables_only_cpu_sleep_0_and_keeps_wfi(tmp_path) -> None:
    # Regression: the first version looked for state0 inside state1's directory
    # (`${p%/*}/state0`), read an empty name, and refused on the real layout.
    root = board(tmp_path)

    result = run(root, "apply")

    assert result.returncode == 0, result.stderr
    assert "APPLIED: cpu-sleep-0 disabled on 4 CPUs" in result.stdout
    after = disables(root)
    assert {k: v for k, v in after.items() if "state1" in k} == dict.fromkeys(
        (f"cpu{c}/cpuidle/state1/disable" for c in range(4)), "1"
    )
    assert all(v == "0" for k, v in after.items() if "state0" in k)
    saved = (tmp_path / "orig").read_text().splitlines()
    assert len(saved) == 4 and all(line.endswith(" 0") for line in saved)


def test_rollback_restores_the_original_values(tmp_path) -> None:
    root = board(tmp_path)
    original = disables(root)
    assert run(root, "apply").returncode == 0
    # A second apply must not overwrite the saved originals with "1".
    assert run(root, "apply").returncode == 0

    result = run(root, "rollback")

    assert result.returncode == 0, result.stderr
    assert "ROLLED BACK" in result.stdout
    assert disables(root) == original
    assert not (tmp_path / "orig").exists()


@pytest.mark.parametrize(
    ("layout", "message"),
    (
        ({"state0": "cpu-sleep-0", "deep": "WFI"}, "state0 is not WFI"),
        ({"state0": "POLL"}, "state0 is not WFI"),
        ({"deep": "cluster-sleep"}, "expected 4 cpu-sleep-0 states, found 0"),
        ({"cpus": 3}, "expected 4 cpu-sleep-0 states, found 3"),
    ),
)
@pytest.mark.parametrize("mode", ("check", "apply"))
def test_unexpected_layouts_fail_closed_without_writing(
    tmp_path, layout, message, mode
) -> None:
    root = board(tmp_path, **layout)
    before = disables(root)

    result = run(root, mode)

    assert result.returncode == 1
    assert message in result.stderr
    assert disables(root) == before
    assert not (tmp_path / "orig").exists()


@NOT_ROOT
def test_a_failed_write_restores_every_core(tmp_path) -> None:
    root = board(tmp_path)
    original = disables(root)
    (root / "cpu2" / "cpuidle" / "state1" / "disable").chmod(0o444)

    result = run(root, "apply")

    assert result.returncode == 1
    assert "write/verify failed" in result.stderr
    assert disables(root) == original


@NOT_ROOT
def test_mutating_modes_require_root_on_the_real_tree(tmp_path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "apply"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "run with sudo" in result.stderr


def test_unknown_mode_is_refused(tmp_path) -> None:
    assert run(board(tmp_path), "enable").returncode == 2
