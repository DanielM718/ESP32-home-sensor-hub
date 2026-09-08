"""Deployment identity and drift detection for the installed Butters tree.

The stabilization pass that introduced this module was triggered by a
deployment that had been hand-patched: `install-beta1` was last run days
earlier, and individual files were copied into /opt/butters afterwards. The
frontend was therefore several commits ahead of the backend it called, so the
Tools page asked for actions the running code had never heard of and the whole
Desktop panel failed with an authorization-shaped error.

Nothing detected that. This module makes it detectable:

* `install-beta1` records the source commit and a digest of everything it
  installed in a DEPLOYMENT file at the install root.
* `describe()` recomputes that digest from the tree that is actually running
  and reports `modified_since_install` when they differ.

The digest deliberately covers only the files that determine behaviour --
Python sources and the browser assets -- so ordinary runtime state, bytecode,
and the virtualenv never register as drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "DEPLOYMENT"

# Bytecode, the virtualenv and runtime state say nothing about behaviour.
_SKIP_DIRECTORIES = frozenset({"__pycache__", ".venv", ".ruff_cache", ".git"})
_DIGEST_SUFFIXES = frozenset({".py", ".js", ".css", ".html"})


def _entries(source: Path, prefix: str = "") -> list[tuple[str, Path]]:
    """(name, path) pairs for the behaviour-determining files under `source`.

    `prefix` places a directory at the name it will have once staged, so a
    package that lives elsewhere in the checkout hashes identically to the
    installed copy.
    """

    if not source.is_dir():
        return []
    found = []
    for path in source.rglob("*"):
        if not path.is_file() or path.suffix not in _DIGEST_SUFFIXES:
            continue
        relative = path.relative_to(source)
        if any(part in _SKIP_DIRECTORIES for part in relative.parts):
            continue
        found.append((prefix + str(relative), path))
    return found


def _digest(entries: list[tuple[str, Path]]) -> str:
    """Hash relative name and content together.

    Hashing the name as well as the bytes means a renamed or removed file
    changes the digest, not just an edited one.
    """

    accumulator = hashlib.sha256()
    for name, path in sorted(entries):
        accumulator.update(name.encode("utf-8"))
        accumulator.update(b"\0")
        accumulator.update(hashlib.sha256(path.read_bytes()).digest())
    return accumulator.hexdigest()


def digest_paths(root: Path) -> tuple[Path, ...]:
    """The behaviour-determining files of an installed tree, in stable order."""

    return tuple(path for _name, path in sorted(_entries(Path(root) / "src")))


def tree_digest(root: Path) -> str:
    """Digest of an installed tree, as published under `root`."""

    return _digest(_entries(Path(root) / "src"))


def checkout_digest(butters_dir: Path) -> str:
    """Digest of a checkout, arranged the way `install-beta1` stages it.

    The server imports butters_agent.protocol, the wire contract shared with
    the Windows Desktop Agent, and that package lives in a sibling checkout
    directory rather than under butters/src. The installer stages it into
    src/butters_agent, so a checkout has to be hashed with it mapped to the
    same name or every correct deployment would look like drift.
    """

    butters_dir = Path(butters_dir)
    entries = _entries(butters_dir / "src")
    sibling = butters_dir.parent / "butters-agent/src/butters_agent"
    entries += _entries(sibling, prefix="butters_agent/")
    return _digest(entries)


def describe(root: Path) -> dict[str, object]:
    """Report what is installed and whether it still matches the install.

    Never raises. A missing or unreadable manifest is reported as an unknown
    deployment rather than taking the daemon down, because this is diagnostic
    information on an administrative page.
    """

    root = Path(root)
    live = tree_digest(root)
    record: dict[str, object] = {}
    manifest = root / MANIFEST_NAME
    try:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            record = {}
    except (OSError, ValueError):
        record = {}
    recorded = record.get("tree_digest")
    if not isinstance(recorded, str) or not recorded:
        status = "unknown"
        modified = None
    elif recorded == live:
        status = "installed"
        modified = False
    else:
        status = "modified_since_install"
        modified = True
    return {
        "status": status,
        "modified_since_install": modified,
        "commit": record.get("commit") or "unknown",
        "branch": record.get("branch") or "unknown",
        "installed_at": record.get("installed_at") or "unknown",
        "source": record.get("source") or "unknown",
        "tree_digest": live,
        "recorded_tree_digest": recorded if isinstance(recorded, str) else None,
        "file_count": len(digest_paths(root)),
        "root": str(root),
    }


def asset_version(root: Path) -> str:
    """Short cache-busting token for the browser assets.

    Derived from the live tree, so a restart after any deployment -- including
    a hand-patched one -- serves a new URL and no browser can stay on obsolete
    JavaScript.
    """

    return tree_digest(root)[:16]
