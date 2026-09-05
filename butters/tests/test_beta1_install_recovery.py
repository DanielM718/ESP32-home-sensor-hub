"""Installer regressions for interrupted deployments and the published venv.

Three defects observed on the live deployment:

* Publishing a new tree takes two renames, and the installer's ``cleanup`` EXIT
  trap was armed across the window between them. A signal there deleted the
  staged tree while the old one sat under another name, leaving production with
  no ``/opt/butters`` at all and no code path that put it back.
* ``rm -rf ${previous_dir}`` ran *before* the swap, so there was no rollback
  copy on disk during exactly the window that might need one.
* The virtualenv is built under the staging path and then renamed, so every
  console script in the published venv kept a shebang naming a directory that
  no longer exists. On the live Pi ``/opt/butters/.venv/bin/pip`` began
  ``#!/opt/butters.staging.81977/.venv/bin/python3``.

Everything here runs against pytest's tmp_path. Nothing reads, requires or
touches a production path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install-beta1"
PASSKEY = Path(__file__).resolve().parents[1] / "scripts" / "butters-passkey"
VERIFY = Path(__file__).resolve().parents[1] / "scripts" / "verify-beta1"


def _source_and_run(body: str, *arguments: str) -> subprocess.CompletedProcess:
    script = 'source "$1" || exit 90\nshift\n' + body
    return subprocess.run(
        ["bash", "-c", script, "bash", str(INSTALLER), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the published virtualenv --------------------------------------------


def _staged_venv(root: Path) -> None:
    binaries = root / ".venv" / "bin"
    binaries.mkdir(parents=True)
    (binaries / "pip").write_text(
        f"#!{root}/.venv/bin/python3\n# -*- coding: utf-8 -*-\nimport sys\n",
        encoding="utf-8",
    )
    (binaries / "uvicorn").write_text(
        f"#!{root}/.venv/bin/python\nimport uvicorn\n", encoding="utf-8"
    )
    (binaries / "python").symlink_to("/usr/bin/python3")
    (root / ".venv" / "pyvenv.cfg").write_text(
        f"home = /usr/bin\ncommand = /usr/bin/python3 -m venv {root}/.venv\n",
        encoding="utf-8",
    )


def test_console_scripts_point_at_the_path_they_will_be_published_at(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "butters.staging.4242"
    staging.mkdir()
    _staged_venv(staging)

    result = _source_and_run(
        'retarget_runtime_venv "$1" "$2"', str(staging), "/opt/butters"
    )

    assert result.returncode == 0, result.stderr
    assert (staging / ".venv" / "bin" / "pip").read_text().splitlines()[0] == (
        "#!/opt/butters/.venv/bin/python3"
    )
    assert (staging / ".venv" / "bin" / "uvicorn").read_text().splitlines()[0] == (
        "#!/opt/butters/.venv/bin/python"
    )
    assert str(staging) not in (staging / ".venv" / "pyvenv.cfg").read_text()


def test_retargeting_leaves_the_interpreter_symlink_alone(tmp_path: Path) -> None:
    """bin/python is a symlink to the system interpreter, not a wrapper."""

    staging = tmp_path / "butters.staging.4242"
    staging.mkdir()
    _staged_venv(staging)

    _source_and_run('retarget_runtime_venv "$1" "$2"', str(staging), "/opt/butters")

    assert (staging / ".venv" / "bin" / "python").is_symlink()
    assert (staging / ".venv" / "bin" / "python").readlink() == Path(
        "/usr/bin/python3"
    )


def test_retargeting_does_not_rewrite_an_unrelated_shebang(tmp_path: Path) -> None:
    staging = tmp_path / "butters.staging.4242"
    staging.mkdir()
    _staged_venv(staging)
    foreign = staging / ".venv" / "bin" / "system-tool"
    foreign.write_text("#!/usr/bin/env python3\nprint(1)\n", encoding="utf-8")

    _source_and_run('retarget_runtime_venv "$1" "$2"', str(staging), "/opt/butters")

    assert foreign.read_text().splitlines()[0] == "#!/usr/bin/env python3"


# --- the swap window ------------------------------------------------------

SWAP_SOURCE = INSTALLER.read_text(encoding="utf-8")


def _swap_block() -> str:
    start = SWAP_SOURCE.index("# 4. Swap.")
    return SWAP_SOURCE[start : start + 1600]


def test_the_exit_trap_is_disarmed_across_the_swap_window() -> None:
    """cleanup() rm -rf's the staged tree; it must not fire mid-swap."""

    block = _swap_block()
    disarm = block.index("trap - EXIT")
    first_rename = block.index('mv "${install_dir}" "${previous_dir}"')
    rearm = block.index("trap cleanup EXIT")

    assert disarm < first_rename < rearm


def test_the_rollback_copy_survives_the_swap_window() -> None:
    """Deleting ${previous_dir} before the swap leaves nothing to roll back to."""

    block = _swap_block()

    assert 'rm -rf "${previous_dir}"' not in block
    assert 'rm -rf "${superseded_dir}"' in block
    assert (
        block.index('rm -rf "${superseded_dir}"')
        < block.index('mv "${install_dir}" "${previous_dir}"')
    )


def test_a_failed_publish_puts_the_previous_tree_back() -> None:
    block = _swap_block()

    assert 'if ! mv "${staging_dir}" "${install_dir}"; then' in block
    assert 'mv "${previous_dir}" "${install_dir}"' in block


def test_the_installer_no_longer_calls_the_two_step_swap_atomic() -> None:
    """The old comment asserted a guarantee rename(2) cannot give for a dir."""

    block = _swap_block()

    assert "Renames are atomic, so no mixed tree is ever importable" not in block
    assert "cannot replace a non-empty directory" in block


def test_stale_staging_trees_from_an_interrupted_run_are_swept() -> None:
    assert "for stale in /opt/butters.staging.*" in SWAP_SOURCE
    assert 'rm -rf "${stale}"' in SWAP_SOURCE


# --- executable rehearsal of the swap ordering ----------------------------


SWAP_REHEARSAL = """
set -Eeuo pipefail
root="$1"
install_dir="${root}/butters"
staging_dir="${root}/butters.staging.$$"
previous_dir="${root}/butters.previous"
superseded_dir="${root}/butters.superseded"
cleanup() { rm -rf "${staging_dir}"; }
trap cleanup EXIT

mkdir -p "${install_dir}" && echo current > "${install_dir}/marker"
mkdir -p "${previous_dir}" && echo old > "${previous_dir}/marker"
mkdir -p "${staging_dir}" && echo new > "${staging_dir}/marker"

trap - EXIT
retired=0
if [[ -d "${install_dir}" ]]; then
  rm -rf "${superseded_dir}"
  if [[ -d "${previous_dir}" ]]; then
    mv "${previous_dir}" "${superseded_dir}"
  fi
  mv "${install_dir}" "${previous_dir}"
  retired=1
fi
# Simulate the second rename failing.
if ! ${FAIL_PUBLISH:-false}; then
  mv "${staging_dir}" "${install_dir}"
else
  if [[ "${retired}" == "1" ]]; then
    mv "${previous_dir}" "${install_dir}"
    [[ -d "${superseded_dir}" ]] && mv "${superseded_dir}" "${previous_dir}"
  fi
  echo PUBLISH-FAILED
  exit 3
fi
rm -rf "${superseded_dir}"
trap cleanup EXIT
echo PUBLISHED
"""


@pytest.mark.parametrize("fail", [False, True])
def test_the_swap_ordering_always_leaves_a_usable_tree(
    tmp_path: Path, fail: bool
) -> None:
    """Drive the real ordering, including the failure branch."""

    root = tmp_path / "opt"
    root.mkdir()
    result = subprocess.run(
        ["bash", "-c", SWAP_REHEARSAL, "bash", str(root)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "FAIL_PUBLISH": "true" if fail else "false"},
    )

    install = root / "butters" / "marker"
    assert install.is_file(), result.stderr
    if fail:
        assert result.returncode == 3
        assert install.read_text().strip() == "current"
        assert (root / "butters.previous" / "marker").read_text().strip() == "old"
    else:
        assert result.returncode == 0
        assert install.read_text().strip() == "new"
        assert (root / "butters.previous" / "marker").read_text().strip() == "current"
    assert not (root / "butters.superseded").exists()


# --- wrappers and verification -------------------------------------------


def test_every_wrapper_puts_the_source_tree_on_the_path() -> None:
    """No pyproject.toml exists, so the venv has no butters package.

    butters-passkey omitted this, which broke the documented first-credential
    bootstrap with ModuleNotFoundError on the deployed host.
    """

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    wrappers = [
        path
        for path in sorted(scripts.iterdir())
        if path.is_file()
        and "venv/bin/python" in path.read_text(encoding="utf-8", errors="ignore")
        and not path.name.startswith(("install-", "download-"))
        and path.name not in {"test-butters", "verify-beta1"}
    ]

    assert wrappers, "no wrapper scripts were found to check"
    for wrapper in wrappers:
        assert "PYTHONPATH" in wrapper.read_text(encoding="utf-8"), wrapper.name


def test_passkey_bootstrap_exports_the_source_tree() -> None:
    body = PASSKEY.read_text(encoding="utf-8")

    assert "PYTHONPATH" in body
    assert "butters.auth_cli" in body


def test_verification_still_reports_when_the_daemon_is_down() -> None:
    """set -e plus an unguarded curl aborted before the explanatory output.

    The systemd status and Tailscale serve output are the whole reason to run
    this script when /healthz fails.
    """

    body = VERIFY.read_text(encoding="utf-8")
    health = body.index("/healthz")
    status = body.index("butters-web.service")

    assert "probe " in body
    assert body.count("probe ") >= 4
    assert health < status
    # The unguarded form the abort came from must not reappear.
    assert "\ncurl --fail" not in body
