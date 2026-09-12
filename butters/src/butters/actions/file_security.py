"""Shared fail-closed checks for service-readable secret files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def require_private_regular_file(path: Path, label: str) -> None:
    """Require a root/runtime-owned file with no unsafe permission bits.

    Group read is permitted because the deployed secrets are root:butters 0640.
    Symlinks and files owned by unrelated accounts are rejected.
    """

    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid not in {0, os.geteuid()}
        or details.st_mode & 0o027
    ):
        raise ValueError(f"unsafe_{label}")
