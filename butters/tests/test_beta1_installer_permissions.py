"""Installer regression: a staged tree must be sealed for the service user.

rsync -a reproduces the checkout's private modes (0700 directories, 0600 files).
Assigning root:root and clearing group write leaves that tree readable only by
root, so the unprivileged `butters` unit cannot chdir into /opt/butters, exec
the interpreter, or import butters.web.app. These tests drive the installer's
real normalization helper against a tree with exactly those restrictive modes.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path

import pytest


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install-beta1"

# Every regular file the staged tree contains, as (relative path, source mode).
# The executable entries mirror real staged content: helper scripts at 0700,
# venv console scripts at 0755, and sherpa/websockets native libraries at 0711.
DATA_FILES = {
    "src/butters/__init__.py": 0o600,
    "src/butters/web/app.py": 0o600,
    "src/butters/web/static/assets/app.js": 0o600,
    "src/butters/web/static/index.html": 0o600,
    "models/vits-piper/model.onnx": 0o600,
    "config/assistant.toml": 0o664,
    # compileall runs before sealing, so its output must be sealed as well.
    "src/butters/web/__pycache__/app.cpython-313.pyc": 0o644,
}
EXECUTABLE_FILES = {
    "scripts/butters-web": 0o700,
    ".venv/bin/uvicorn": 0o755,
    ".venv/lib/python3.13/site-packages/sherpa_onnx/lib/_sherpa_onnx.so": 0o711,
}
# Directory modes seen in a real checkout: mostly 0700, some world-traversable.
DIRECTORIES = {
    "src": 0o700,
    "src/butters": 0o700,
    "src/butters/web": 0o700,
    "src/butters/web/__pycache__": 0o700,
    "src/butters/web/static": 0o700,
    "src/butters/web/static/assets": 0o700,
    "scripts": 0o700,
    "config": 0o700,
    "models": 0o700,
    "models/vits-piper": 0o755,
    ".venv": 0o700,
    ".venv/bin": 0o700,
    ".venv/lib/python3.13/site-packages/sherpa_onnx/lib": 0o700,
}

EXTERNAL_LINK_TARGET = Path("/usr/bin/python3")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _staged_tree(tmp_path: Path) -> Path:
    """Build a staging tree with the checkout's restrictive modes."""

    root = tmp_path / "butters.staging"
    root.mkdir()
    for relative in DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)
    for relative, mode in {**DATA_FILES, **EXECUTABLE_FILES}.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"fixture\n")
        target.chmod(mode)

    # Symlinks the installer must not follow out of the tree, plus a dangling
    # link, which is why chown uses -h and chmod is restricted to -type d/f.
    (root / ".venv/bin/python3").symlink_to(EXTERNAL_LINK_TARGET)
    (root / ".venv/bin/python").symlink_to("python3")
    (root / "scripts/broken").symlink_to("/nonexistent/dangling")

    # Directory modes last: creating children would otherwise require write.
    for relative, mode in DIRECTORIES.items():
        (root / relative).chmod(mode)
    root.chmod(0o700)
    return root


def _normalize(tree: Path) -> None:
    """Invoke the installer's own helper, sourced rather than reimplemented."""

    user = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; normalize_application_tree "$2" "$3" "$4"',
            "bash",
            str(INSTALLER),
            str(tree),
            user,
            group,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def sealed_tree(tmp_path: Path) -> Path:
    tree = _staged_tree(tmp_path)
    # The failure being regressed: as staged, the service group has no access.
    assert not _mode(tree) & stat.S_IXGRP
    assert not _mode(tree / "src/butters/web/app.py") & stat.S_IRGRP
    _normalize(tree)
    return tree


def test_sourcing_the_installer_performs_no_installation() -> None:
    """The helper is reachable without the script attempting to install."""

    completed = subprocess.run(
        ["bash", "-c", 'source "$1"; declare -F normalize_application_tree', "bash", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "normalize_application_tree" in completed.stdout
    assert not Path("/opt/butters").exists()


def test_restrictive_source_modes_are_normalized(sealed_tree: Path) -> None:
    for relative in DIRECTORIES:
        assert _mode(sealed_tree / relative) == 0o750, relative
    assert _mode(sealed_tree) == 0o750
    for relative in DATA_FILES:
        assert _mode(sealed_tree / relative) == 0o640, relative


def test_ordinary_files_do_not_become_executable(sealed_tree: Path) -> None:
    for relative in DATA_FILES:
        mode = _mode(sealed_tree / relative)
        assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), relative


def test_existing_executables_retain_execution(sealed_tree: Path) -> None:
    for relative in EXECUTABLE_FILES:
        mode = _mode(sealed_tree / relative)
        assert mode == 0o750, relative
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP, relative
        # A native library must stay readable as well as executable.
        assert mode & stat.S_IRGRP, relative


def test_service_group_can_traverse_and_read(sealed_tree: Path) -> None:
    """Every path the unit needs: chdir, exec the interpreter, import the app."""

    for relative in ("", "src", "src/butters", "src/butters/web", ".venv", ".venv/bin"):
        mode = _mode(sealed_tree / relative) if relative else _mode(sealed_tree)
        assert mode & stat.S_IXGRP, relative or "."
        assert mode & stat.S_IRGRP, relative or "."
    assert _mode(sealed_tree / "src/butters/web/app.py") & stat.S_IRGRP
    assert _mode(sealed_tree / ".venv/bin/uvicorn") & stat.S_IXGRP
    # .venv/bin/python is a symlink chain ending outside the tree; the service
    # user's ability to exec it depends on the directory being traversable.
    assert (sealed_tree / ".venv/bin/python").resolve() == EXTERNAL_LINK_TARGET.resolve()


def test_no_world_access_and_no_group_write(sealed_tree: Path) -> None:
    seen: set[str] = set()
    for current, directories, files in os.walk(sealed_tree):
        for name in (*directories, *files):
            path = Path(current) / name
            if path.is_symlink():
                continue
            mode = _mode(path)
            assert not mode & stat.S_IRWXO, f"world access on {path}"
            assert not mode & stat.S_IWGRP, f"group write on {path}"
            assert not mode & (stat.S_ISUID | stat.S_ISGID), f"setid bit on {path}"
            seen.add(str(path.relative_to(sealed_tree)))
    assert not _mode(sealed_tree) & stat.S_IRWXO
    # The walk really covered the tree, including parents created implicitly at
    # the process umask rather than at an explicitly restrictive mode.
    assert seen >= {*DATA_FILES, *EXECUTABLE_FILES, *DIRECTORIES}
    assert ".venv/lib/python3.13/site-packages" in seen


def test_symlinks_are_not_followed_out_of_the_tree(sealed_tree: Path) -> None:
    before = EXTERNAL_LINK_TARGET.stat()
    assert (sealed_tree / ".venv/bin/python3").is_symlink()
    assert (sealed_tree / "scripts/broken").is_symlink()
    # A dangling link neither aborted normalization nor was materialized.
    assert not (sealed_tree / "scripts/broken").exists()
    after = EXTERNAL_LINK_TARGET.stat()
    assert (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) == (
        after.st_uid,
        after.st_gid,
        stat.S_IMODE(after.st_mode),
    )


def test_normalization_precedes_the_swap_and_service_start() -> None:
    """Ordering invariant: the tree is sealed before it is ever published."""

    lines = INSTALLER.read_text(encoding="utf-8").splitlines()

    def index_of(needle: str) -> int:
        matches = [i for i, line in enumerate(lines) if needle in line]
        assert len(matches) == 1, f"expected exactly one {needle!r}, got {matches}"
        return matches[0]

    compile_step = index_of("-m compileall")
    seal = index_of('normalize_application_tree "${staging_dir}"')
    swap = index_of('mv "${staging_dir}" "${install_dir}"')
    first_systemctl = min(i for i, line in enumerate(lines) if "systemctl" in line)

    # compileall writes __pycache__ into the staging tree, so it must precede
    # sealing; sealing must precede the atomic swap and any service action.
    assert compile_step < seal < swap < first_systemctl

    # Nothing may write to the staging tree after it has been sealed, otherwise
    # the published tree could still contain root-only paths.
    after_seal = [line for line in lines[seal + 1 :] if "${staging_dir}" in line]
    assert after_seal == ['mv "${staging_dir}" "${install_dir}"']
