"""Butters-side copy of the audited NAS Agent v1 signed wire primitives."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid

PROTOCOL_VERSION = 1
ACTION_SCHEMA_VERSION = 1
MAX_FRAME = 32768
SCHEMAS = {
    "nas.agent.status": frozenset(),
    "nas.system.status": frozenset(),
    "nas.jellyfin.status": frozenset(),
    "nas.system.shutdown": frozenset(),
}


class ProtocolError(ValueError):
    pass


def parameters(action: object, values: object) -> dict[str, object]:
    if not isinstance(action, str) or action not in SCHEMAS:
        raise ProtocolError("invalid_action")
    if type(values) is not dict or set(values) != SCHEMAS[action]:
        raise ProtocolError("invalid_parameter")
    return dict(values)


def canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ProtocolError("malformed_message") from None


def decode(raw: object) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME:
        raise ProtocolError("malformed_message")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("malformed_message")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if type(value) is not dict:
            raise ValueError()
        return value
    except (ValueError, RecursionError):
        raise ProtocolError("malformed_message") from None


def sign(frame: dict[str, object], key: bytes) -> dict[str, object]:
    unsigned = {name: value for name, value in frame.items() if name != "sig"}
    return {
        **unsigned,
        "sig": hmac.new(key, canonical(unsigned), hashlib.sha256).hexdigest(),
    }


def verify(
    frame: dict[str, object],
    key: bytes,
    connection_id: str,
    *,
    now: float | None = None,
) -> dict[str, object]:
    stamp = frame.get("issued_at")
    current = time.time() if now is None else now
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or abs(current - stamp) > 90
    ):
        raise ProtocolError("stale_request")
    if frame.get("connection_id") != connection_id:
        raise ProtocolError("superseded_connection")
    signature = frame.get("sig")
    expected = sign(frame, key)["sig"]
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise ProtocolError("invalid_signature")
    return frame


def envelope(kind: str, connection_id: str, **fields: object) -> dict[str, object]:
    return {
        "type": kind,
        "connection_id": connection_id,
        "issued_at": time.time(),
        **fields,
    }


def validate_request_id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value, version=4)) != value:
            raise ValueError()
    except (TypeError, ValueError, AttributeError):
        raise ProtocolError("invalid_request_id") from None
    return value
