"""Installer regression: a staged tree must be sealed for the service user.

rsync -a reproduces the checkout's private modes (0700 directories, 0600 files).
Assigning root:root and clearing group write leaves that tree readable only by
root, so the unprivileged `butters` unit cannot chdir into /opt/butters, exec
the interpreter, or import butters.web.app. These tests drive the installer's
real normalization helper against a tree with exactly those restrictive modes.

Everything here is confined to pytest's tmp_path. No test inspects, requires, or
depends on a production path, so the suite passes both before and after Butters
is installed.
"""

from __future__ import annotations

import grp
import os
import pwd
import stat
import subprocess
from pathlib import Path
from typing import NamedTuple

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
    # compileall runs before the final seal, so its output must be sealed too.
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
# Relative internal links, plus a dangling one. The absolute link that escapes
# the tree is added separately because its target is built per-test.
RELATIVE_SYMLINKS = {
    ".venv/bin/python": "python3",
    ".venv/bin/python3.13": "python3",
    ".venv/lib64": "lib",
    "scripts/broken": "/nonexistent/dangling",
}
EXTERNAL_LINK = ".venv/bin/python3"
# The external target is deliberately 0755. If a future implementation followed
# symlinks (find -L, chmod -R), it would be rewritten to 0750 and detected.
EXTERNAL_TARGET_MODE = 0o755


class Sealed(NamedTuple):
    """A normalized tree plus metadata captured *before* normalization ran."""

    root: Path
    external: Path
    external_before: tuple[int, int, int]
    external_bytes_before: bytes
    links_before: dict[str, tuple[int, int, int]]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _ids(path: Path) -> tuple[int, int, int]:
    """lstat-based (mode, uid, gid); never follows a symlink."""

    info = path.lstat()
    return (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)


def _snapshot(root: Path) -> dict[str, tuple[int, int, int, int]]:
    entries: dict[str, tuple[int, int, int, int]] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        entries[str(path.relative_to(root.parent))] = (
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_size,
        )
    return entries


def _external_target(tmp_path: Path) -> Path:
    """An executable file outside the staged tree, standing in for /usr/bin/python3."""

    target = tmp_path / "external" / "python3"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(EXTERNAL_TARGET_MODE)
    return target


def _staged_tree(tmp_path: Path, external: Path) -> Path:
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
    (root / EXTERNAL_LINK).symlink_to(external)
    assert (root / EXTERNAL_LINK).readlink().is_absolute()
    for relative, target_name in RELATIVE_SYMLINKS.items():
        (root / relative).symlink_to(target_name)

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
def sealed(tmp_path: Path) -> Sealed:
    external = _external_target(tmp_path)
    tree = _staged_tree(tmp_path, external)
    # The failure being regressed: as staged, the service group has no access.
    assert not _mode(tree) & stat.S_IXGRP
    assert not _mode(tree / "src/butters/web/app.py") & stat.S_IRGRP

    # Captured before normalization so the comparisons below are not tautologies.
    external_before = _ids(external)
    external_bytes_before = external.read_bytes()
    links_before = {
        relative: _ids(tree / relative)
        for relative in (EXTERNAL_LINK, *RELATIVE_SYMLINKS)
    }

    _normalize(tree)
    return Sealed(tree, external, external_before, external_bytes_before, links_before)


def test_sourcing_defines_the_helper_without_installing(tmp_path: Path) -> None:
    """Sourcing defines helpers, returns cleanly, and mutates nothing."""

    fixture = tmp_path / "untouched"
    fixture.mkdir()
    (fixture / "sentinel").write_text("sentinel\n", encoding="utf-8")
    (fixture / "sentinel").chmod(0o600)
    fixture.chmod(0o700)
    before = _snapshot(fixture)

    script = (
        'trap "echo CALLER-EXIT-TRAP-RAN" EXIT\n'
        'source "$1"; echo "rc=$?"\n'
        'declare -F normalize_application_tree >/dev/null && echo HELPER-DEFINED\n'
        "trap -p EXIT\n"
    )
    completed = subprocess.run(
        ["bash", "-c", script, "bash", str(INSTALLER)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "HELPER-DEFINED" in completed.stdout
    assert "rc=0" in completed.stdout
    # The installer's own `trap cleanup EXIT` sits after the source guard, so
    # sourcing must neither register it nor displace the caller's EXIT trap.
    assert "cleanup" not in completed.stdout
    assert "CALLER-EXIT-TRAP-RAN" in completed.stdout
    assert _snapshot(fixture) == before


def test_restrictive_source_modes_are_normalized(sealed: Sealed) -> None:
    for relative in DIRECTORIES:
        assert _mode(sealed.root / relative) == 0o750, relative
    assert _mode(sealed.root) == 0o750
    for relative in DATA_FILES:
        assert _mode(sealed.root / relative) == 0o640, relative


def test_ordinary_files_do_not_become_executable(sealed: Sealed) -> None:
    for relative in DATA_FILES:
        mode = _mode(sealed.root / relative)
        assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH), relative


def test_existing_executables_retain_execution(sealed: Sealed) -> None:
    for relative in EXECUTABLE_FILES:
        mode = _mode(sealed.root / relative)
        assert mode == 0o750, relative
        assert mode & stat.S_IXUSR and mode & stat.S_IXGRP, relative
        # A native library must stay readable as well as executable.
        assert mode & stat.S_IRGRP, relative


def test_service_group_can_traverse_and_read(sealed: Sealed) -> None:
    """Every path the unit needs: chdir, exec the interpreter, import the app."""

    for relative in ("", "src", "src/butters", "src/butters/web", ".venv", ".venv/bin"):
        mode = _mode(sealed.root / relative) if relative else _mode(sealed.root)
        assert mode & stat.S_IXGRP, relative or "."
        assert mode & stat.S_IRGRP, relative or "."
    assert _mode(sealed.root / "src/butters/web/app.py") & stat.S_IRGRP
    assert _mode(sealed.root / ".venv/bin/uvicorn") & stat.S_IXGRP
    # .venv/bin/python is a relative link to the absolute link that leaves the
    # tree; exec depends on the directories being traversable and the external
    # target keeping its execute bit.
    assert (sealed.root / ".venv/bin/python").resolve() == sealed.external.resolve()
    assert _mode(sealed.external) & stat.S_IXUSR


def test_no_world_access_and_no_group_write(sealed: Sealed) -> None:
    seen: set[str] = set()
    for current, directories, files in os.walk(sealed.root):
        for name in (*directories, *files):
            path = Path(current) / name
            if path.is_symlink():
                continue
            mode = _mode(path)
            assert not mode & stat.S_IRWXO, f"world access on {path}"
            assert not mode & stat.S_IWGRP, f"group write on {path}"
            assert not mode & (stat.S_ISUID | stat.S_ISGID), f"setid bit on {path}"
            seen.add(str(path.relative_to(sealed.root)))
    assert not _mode(sealed.root) & stat.S_IRWXO
    # The walk really covered the tree, including parents created implicitly at
    # the process umask rather than at an explicitly restrictive mode.
    assert seen >= {*DATA_FILES, *EXECUTABLE_FILES, *DIRECTORIES}
    assert ".venv/lib/python3.13/site-packages" in seen


def test_external_symlink_target_is_untouched(sealed: Sealed) -> None:
    """The escaping absolute link must not be dereferenced by chown or chmod."""

    assert _ids(sealed.external) == sealed.external_before
    assert sealed.external.read_bytes() == sealed.external_bytes_before
    assert stat.S_ISREG(sealed.external.lstat().st_mode)
    # Explicitly: a dereferencing implementation (find -L, chmod -R) would have
    # rewritten this executable target from 0755 to 0750.
    assert _mode(sealed.external) == EXTERNAL_TARGET_MODE


def test_symlinks_survive_normalization(sealed: Sealed) -> None:
    for relative in (EXTERNAL_LINK, *RELATIVE_SYMLINKS):
        link = sealed.root / relative
        assert link.is_symlink(), relative
    # A dangling link neither aborted normalization nor was materialized.
    assert not (sealed.root / "scripts/broken").exists()
    assert (sealed.root / "scripts/broken").is_symlink()


def test_symlink_ownership_final_state(sealed: Sealed) -> None:
    """Assert the links' own lstat ownership after normalization.

    This is a final-state assertion, not proof that `chown -h` *changed* the
    links: the tests run unprivileged and can only chown to the invoking user,
    which already owns the fixture, so no observable transition exists without
    root. What it does establish is that the links themselves carry the expected
    ownership and that their modes were left alone.
    """

    uid, gid = os.getuid(), os.getgid()
    for relative in (EXTERNAL_LINK, *RELATIVE_SYMLINKS):
        link_mode, link_uid, link_gid = _ids(sealed.root / relative)
        assert (link_uid, link_gid) == (uid, gid), relative
        # chmod is restricted to -type d/-type f, so link modes are unchanged.
        assert link_mode == sealed.links_before[relative][0], relative


def test_installer_ordering_freezes_then_seals_then_swaps() -> None:
    """rsync < freeze < compileall < final seal < atomic swap < systemctl."""

    lines = INSTALLER.read_text(encoding="utf-8").splitlines()

    def index_of(needle: str) -> int:
        matches = [i for i, line in enumerate(lines) if needle in line]
        assert len(matches) == 1, f"expected exactly one {needle!r}, got {matches}"
        return matches[0]

    rsync = index_of("rsync -a --delete")
    freeze = index_of('chown -h -R root:butters "${staging_dir}"')
    compile_step = index_of("-m compileall")
    seal = index_of('normalize_application_tree "${staging_dir}"')
    swap = index_of('mv "${staging_dir}" "${install_dir}"')
    first_systemctl = min(i for i, line in enumerate(lines) if "systemctl" in line)

    # The freeze must land between staging and the first time root executes
    # anything out of the staged tree, so the snapshot cannot be swapped under
    # root by the unprivileged user who owns the source checkout.
    assert rsync < freeze < compile_step < seal < swap < first_systemctl

    # Nothing may write to the staging tree after it has been sealed, otherwise
    # the published tree could still contain root-only or world-readable paths.
    # Renaming the sealed tree into place is the one permitted use; the publish
    # is wrapped in a failure check that restores the previous tree, so match on
    # the rename rather than on the whole line.
    after_seal = [line.strip() for line in lines[seal + 1 :] if "${staging_dir}" in line]
    assert after_seal == ['if ! mv "${staging_dir}" "${install_dir}"; then']
