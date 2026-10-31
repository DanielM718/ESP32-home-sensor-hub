"""Small durable records for KR260 recovery safety, independent of the job store.

Each record is one JSON file in the Butters state directory, written atomically
(temporary file, fsync, rename, fsync of the directory) with a SHA-256 of its
canonical body. A file that is missing reads as None; a file that is truncated,
unparsable, of another schema, or whose checksum does not match reads as
``CORRUPT``. Callers treat both as "unknown", which always means "wait".

``update`` is a read-modify-write under an exclusive lock: a per-path thread
lock plus ``flock`` on a sidecar lock file, for other processes. Writers use
unique temporary files and write until every byte is out. A full disk or a
read-only directory raises OSError and leaves the previous record intact.

The checksum detects accidental damage only. Anyone who can write the state
directory can forge a record, or restore an older one.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

SCHEMA: Final = 1
CORRUPT: Final = object()
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _digest(body: dict[str, object]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SafetyRecord:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> dict[str, object] | None | object:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            return CORRUPT
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return CORRUPT
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "body", "sha256"}
            or document["schema"] != SCHEMA
            or not isinstance(document["body"], dict)
            or document["sha256"] != _digest(document["body"])
        ):
            return CORRUPT
        return document["body"]

    def write(self, body: dict[str, object]) -> None:
        """Replace the record atomically; raises OSError if it cannot."""

        document = {"schema": SCHEMA, "body": body, "sha256": _digest(body)}
        data = json.dumps(document, sort_keys=True).encode("utf-8")
        # Unique per writer (mkstemp creates it 0600), never shared.
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def update(
        self,
        change: Callable[[dict[str, object] | None | object], dict[str, object] | None],
    ) -> dict[str, object] | None:
        """Read, change and write under an exclusive lock.

        ``change`` receives the current body (or None / CORRUPT) and returns
        the new body, or None to leave the record as it is. Raises OSError if
        the lock or the write fails; the record is then unchanged.
        """

        with _thread_lock(self.path):
            lock_path = self.path.with_name(f".{self.path.name}.lock")
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                new = change(self.read())
                if new is not None:
                    self.write(new)
                return new
            finally:
                os.close(descriptor)  # releases the flock


def _thread_lock(path: Path) -> threading.Lock:
    key = Path(path).absolute()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


def finite_time(value: object) -> float | None:
    """A stored wall-clock time, or None if it is not a finite number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None
