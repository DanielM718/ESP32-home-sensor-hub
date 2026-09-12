"""Shared signed wire format for the standalone Desktop Agent."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import uuid
from collections import OrderedDict

PROTOCOL_VERSION = 1
ACTION_SCHEMA_VERSION = 1
MAX_FRAME = 32768
NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
SCHEMAS = {
    "desktop.agent.status": set(),
    "desktop.session.status": set(),
    "desktop.app.list": set(),
    "desktop.app.status": {"app"},
    "desktop.app.launch": {"app"},
}
MUTATIONS = frozenset({"desktop.app.launch"})


class ProtocolError(ValueError):
    """Only enumerated errors cross the transport/log boundary."""


def parameters(action, values):
    if not isinstance(action, str) or action not in SCHEMAS:
        raise ProtocolError("invalid_action")
    if type(values) is not dict or set(values) != SCHEMAS[action]:
        raise ProtocolError("invalid_parameter")
    if any(
        not isinstance(value, str) or not NAME.fullmatch(value)
        for value in values.values()
    ):
        raise ProtocolError("invalid_parameter")
    return dict(values)


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def decode(raw):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME:
        raise ProtocolError("malformed_message")

    def unique(pairs):
        result = {}
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


def sign(frame, key):
    unsigned = {name: value for name, value in frame.items() if name != "sig"}
    signature = hmac.new(key, canonical(unsigned), hashlib.sha256).hexdigest()
    return {**unsigned, "sig": signature}


def verify(frame, key, connection_id, *, now=None):
    now = time.time() if now is None else now
    stamp = frame.get("issued_at")
    # Retain the protocol's +/- 90 second signed-frame issuance window.
    if (
        type(stamp) not in (int, float)
        or not math.isfinite(stamp)
        or abs(now - stamp) > 90
    ):
        raise ProtocolError("stale_request")
    # Bind every signed frame to the server-minted connection identifier.
    if frame.get("connection_id") != connection_id:
        raise ProtocolError("superseded_connection")
    signature = frame.get("sig")
    expected = sign(frame, key)["sig"]
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise ProtocolError("invalid_signature")
    return frame


def envelope(kind, connection_id, **fields):
    return {
        "type": kind,
        "connection_id": connection_id,
        "issued_at": time.time(),
        **fields,
    }


def validate_request_id(value):
    """Return a canonical UUIDv4 request identity or fail closed."""
    try:
        if str(uuid.UUID(value, version=4)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ProtocolError("invalid_request_id") from None
    return value


def request(frame, *, target, now=None):
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
    if set(frame) != allowed or frame["type"] != "request":
        raise ProtocolError("malformed_request")
    if frame["target"] != target:
        raise ProtocolError("wrong_target")
    validate_request_id(frame["request_id"])
    validate_request_id(frame["idempotency_key"])
    parameters(frame["action"], frame["parameters"])
    timeout = frame["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ProtocolError("invalid_timeout")
    age = (time.time() if now is None else now) - frame["issued_at"]
    if abs(age) > min(timeout, 30):
        raise ProtocolError("stale_request")


class ReplayCache:
    """Bounded terminal-result cache; conflicts never execute. No durable queue."""

    def __init__(self, clock=time.monotonic, capacity=512, ttl=300):
        self.clock = clock
        self.capacity = capacity
        self.ttl = ttl
        self.entries = OrderedDict()

    def _prune(self):
        now = self.clock()
        for key, (stamp, _, _) in list(self.entries.items()):
            if now - stamp >= self.ttl:
                del self.entries[key]

    @staticmethod
    def fingerprint(frame):
        fields = {name: frame[name] for name in ("action", "target", "parameters")}
        return hashlib.sha256(canonical(fields)).hexdigest()

    def get(self, frame):
        self._prune()
        fingerprint = self.fingerprint(frame)
        found = None
        for prefix, field in (("r", "request_id"), ("i", "idempotency_key")):
            item = self.entries.get((prefix, frame[field]))
            if item:
                # A reused identity with different work is never treated as a retry.
                if item[1] != fingerprint:
                    raise ProtocolError("duplicate_request")
                found = item[2]
        return found

    def put(self, frame, result):
        for prefix, field in (("r", "request_id"), ("i", "idempotency_key")):
            self.entries[(prefix, frame[field])] = (
                self.clock(),
                self.fingerprint(frame),
                dict(result),
            )
        while len(self.entries) > self.capacity * 2:
            self.entries.popitem(last=False)
