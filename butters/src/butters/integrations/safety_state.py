"""Small durable records for KR260 recovery safety, independent of the job store.

Each record is one JSON file in the Butters state directory, written atomically
(temporary file, fsync, rename, fsync of the directory) with a SHA-256 of its
canonical body. A file that is missing reads as None; a file that is truncated,
unparsable, of another schema, or whose checksum does not match reads as
``CORRUPT``. Callers treat both as "unknown", which always means "wait".
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Final

SCHEMA: Final = 1
CORRUPT: Final = object()


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
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            try:
                os.write(descriptor, data)
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


def finite_time(value: object) -> float | None:
    """A stored wall-clock time, or None if it is not a finite number."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None
