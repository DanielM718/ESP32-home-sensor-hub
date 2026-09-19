"""Closed, signed wire format for the standalone NAS Agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import uuid
from collections import OrderedDict

PROTOCOL_VERSION = 1
ACTION_SCHEMA_VERSION = 2
MAX_FRAME = 32768
SCHEMAS = {
    "nas.agent.status": frozenset(),
    "nas.system.status": frozenset(),
    "nas.jellyfin.status": frozenset(),
    "nas.network.status": frozenset(),
    "nas.jellyfin.sessions": frozenset(),
    "nas.bandwidth.status": frozenset(),
    "nas.system.shutdown": frozenset(),
}
MUTATIONS = frozenset({"nas.system.shutdown"})


class ProtocolError(ValueError):
    """Only enumerated error codes cross the transport/log boundary."""


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
    signature = hmac.new(key, canonical(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "sig": signature}


def verify(
    frame: dict[str, object],
    key: bytes,
    connection_id: str,
    *,
    now: float | None = None,
) -> dict[str, object]:
    now = time.time() if now is None else now
    stamp = frame.get("issued_at")
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or abs(now - stamp) > 90
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
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError("invalid_request_id") from None
    return value


def request(frame: dict[str, object], *, target: str, now: float | None = None) -> None:
    allowed = {
        "type",
        "connection_id",
        "issued_at",
        "sig",
        "request_id",
        "action",
        "target",
        "parameters",
        "idempotency_key",
        "timeout_seconds",
    }
    if set(frame) != allowed or frame.get("type") != "request":
        raise ProtocolError("malformed_request")
    if frame.get("target") != target:
        raise ProtocolError("wrong_target")
    validate_request_id(frame.get("request_id"))
    validate_request_id(frame.get("idempotency_key"))
    parameters(frame.get("action"), frame.get("parameters"))
    timeout = frame.get("timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ProtocolError("invalid_timeout")
    issued_at = frame.get("issued_at")
    assert type(issued_at) in (int, float)
    age = (time.time() if now is None else now) - float(issued_at)
    if abs(age) > min(timeout, 30):
        raise ProtocolError("stale_request")


class ReplayCache:
    """Bounded terminal-result cache; identity conflicts never execute."""

    def __init__(self, clock=time.monotonic, capacity: int = 512, ttl: int = 300):
        self.clock = clock
        self.capacity = capacity
        self.ttl = ttl
        self.entries: OrderedDict[
            tuple[str, object], tuple[float, str, dict[str, object]]
        ] = OrderedDict()

    def _prune(self) -> None:
        now = self.clock()
        for key, (stamp, _, _) in list(self.entries.items()):
            if now - stamp >= self.ttl:
                del self.entries[key]

    @staticmethod
    def fingerprint(frame: dict[str, object]) -> str:
        fields = {name: frame[name] for name in ("action", "target", "parameters")}
        return hashlib.sha256(canonical(fields)).hexdigest()

    def get(self, frame: dict[str, object]) -> dict[str, object] | None:
        self._prune()
        fingerprint = self.fingerprint(frame)
        found = None
        for prefix, field in (("r", "request_id"), ("i", "idempotency_key")):
            item = self.entries.get((prefix, frame[field]))
            if item:
                if item[1] != fingerprint:
                    raise ProtocolError("duplicate_request")
                found = dict(item[2])
        return found

    def put(self, frame: dict[str, object], result: dict[str, object]) -> None:
        for prefix, field in (("r", "request_id"), ("i", "idempotency_key")):
            self.entries[(prefix, frame[field])] = (
                self.clock(),
                self.fingerprint(frame),
                dict(result),
            )
        while len(self.entries) > self.capacity * 2:
            self.entries.popitem(last=False)
