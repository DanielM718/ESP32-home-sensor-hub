"""Installer regression: production dependencies come from reviewed pins.

The installer used to publish a copy of the developer checkout's .venv. That
carried whatever the developer happened to install into production and silently
omitted anything they never installed -- which is how ``webauthn``, a declared
runtime dependency of every passkey ceremony, could reach a deployment missing
entirely and fail closed only when someone first tried to elevate.

These tests drive the installer's own helper, sourced rather than reimplemented,
and assert the static structure that makes the build reproducible. Nothing here
creates a virtualenv, reaches the network, or touches a production path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BUTTERS = Path(__file__).resolve().parents[1]
INSTALLER = BUTTERS / "scripts" / "install-beta1"


def _verify(requirements: Path) -> subprocess.CompletedProcess[str]:
    """Invoke the installer's verify helper against the running interpreter."""

    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; set +e; verify_runtime_dependencies "$2" "$3"',
            "bash",
            str(INSTALLER),
            sys.executable,
            str(requirements),
        ],
        capture_output=True,
        text=True,
        # The failure paths are the point of these tests; returncode is asserted.
        check=False,
    )


def _write(directory: Path, name: str, body: str) -> Path:
    target = directory / name
    target.write_text(body, encoding="utf-8")
    return target


def test_verify_accepts_pins_that_match_the_environment(tmp_path: Path) -> None:
    """Comments, unpinned ranges, and -r includes must all be handled."""

    _write(tmp_path, "extra.txt", "uvicorn==0.35.0\n")
    requirements = _write(
        tmp_path,
        "requirements.txt",
        "# reviewed runtime pins\n"
        "starlette==0.47.3\n"
        "webauthn==3.0.0  # passkey ceremonies\n"
        "\n"
        "-r extra.txt\n"
        "websockets>=15,<16\n",
    )

    result = _verify(requirements)

    assert result.returncode == 0, result.stderr
    # The unpinned range is not counted, so the guard below cannot be satisfied
    # by a file that happens to parse but pins nothing.
    assert "verified 3 pinned runtime dependencies" in result.stdout


def test_verify_rejects_a_missing_runtime_dependency(tmp_path: Path) -> None:
    """The exact regression: a declared dependency absent from the venv."""

    requirements = _write(
        tmp_path, "requirements.txt", "butters-not-a-real-package==1.0\n"
    )

    result = _verify(requirements)

    assert result.returncode != 0
    assert "butters-not-a-real-package==1.0 is not installed" in result.stderr


def test_verify_rejects_a_substituted_version(tmp_path: Path) -> None:
    requirements = _write(tmp_path, "requirements.txt", "starlette==0.0.1\n")

    result = _verify(requirements)

    assert result.returncode != 0
    assert "starlette is" in result.stderr and "expected 0.0.1" in result.stderr


def test_verify_refuses_to_pass_when_no_pins_are_parsed(tmp_path: Path) -> None:
    """A verifier that silently checks nothing is worse than no verifier."""

    requirements = _write(
        tmp_path, "requirements.txt", "# only comments\nwebsockets>=15,<16\n"
    )

    result = _verify(requirements)

    assert result.returncode != 0
    assert "no pinned requirements were parsed" in result.stderr


def test_webauthn_is_reachable_from_the_reviewed_requirements() -> None:
    """requirements.txt must transitively pin the passkey dependency."""

    web = (BUTTERS / "requirements-web.txt").read_text(encoding="utf-8")
    root = (BUTTERS / "requirements.txt").read_text(encoding="utf-8")

    assert "webauthn==3.0.0" in web
    assert "-r requirements-web.txt" in root

    result = _verify(BUTTERS / "requirements.txt")
    assert result.returncode == 0, result.stderr


def test_installer_builds_the_venv_instead_of_copying_the_developer_one() -> None:
    lines = INSTALLER.read_text(encoding="utf-8").splitlines()

    def index_of(needle: str) -> int:
        matches = [i for i, line in enumerate(lines) if needle in line]
        assert len(matches) == 1, f"expected exactly one {needle!r}, got {matches}"
        return matches[0]

    # The developer virtualenv is neither required nor published.
    assert any("--exclude .venv" in line for line in lines)
    assert not any('-x "${butters_dir}/.venv/bin/python"' in line for line in lines)

    freeze = index_of('chown -h -R root:butters "${staging_dir}"')
    build = index_of('build_runtime_venv "${staging_dir}"')
    verify = index_of("verify_runtime_dependencies \\")
    compile_step = index_of("-m compileall")
    seal = index_of('normalize_application_tree "${staging_dir}"')

    # Built from the system interpreter after the snapshot is frozen, verified
    # before anything is compiled, and long before the tree is published.
    assert freeze < build < verify < compile_step < seal
    assert any("/usr/bin/python3 -m venv" in line for line in lines)

    # Every failure path must stop the install rather than publish a partial tree.
    joined = "\n".join(lines)
    assert "Dependency installation failed; nothing was replaced." in joined
    assert "Dependency verification failed; nothing was replaced." in joined


@pytest.mark.parametrize("tool", ["pytest", "ruff"])
def test_development_tooling_is_not_a_declared_runtime_dependency(tool: str) -> None:
    declared = "\n".join(
        (BUTTERS / name).read_text(encoding="utf-8")
        for name in ("requirements.txt", "requirements-web.txt", "requirements-stt.txt")
    )
    assert tool not in declared
