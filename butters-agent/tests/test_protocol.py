from __future__ import annotations

import json
import uuid

import pytest
from butters_agent.protocol import (
    MUTATIONS,
    SCHEMAS,
    ProtocolError,
    ReplayCache,
    canonical,
    decode,
    envelope,
    parameters,
    request,
    sign,
    verify,
)

KEY = b"k" * 32
CONNECTION_ID = "connection"


def signed_request(action="desktop.app.launch", values=None, **changes):
    frame = envelope(
        "request",
        CONNECTION_ID,
        request_id=str(uuid.uuid4()),
        action=action,
        target="desktop",
        parameters={"app": "git_bash"} if values is None else values,
        idempotency_key=str(uuid.uuid4()),
        timeout_seconds=30,
    )
    return sign({**frame, **changes}, KEY)


def test_protocol_serialization_round_trip_is_canonical():
    original = signed_request()
    encoded = canonical(original).decode("ascii")

    assert encoded == json.dumps(
        original,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    assert decode(encoded) == original
    assert verify(decode(encoded), KEY, CONNECTION_ID) == original
    request(original, target="desktop")


def test_signature_binds_every_request_field():
    original = signed_request()
    changes = (
        ("timeout_seconds", 29),
        ("idempotency_key", str(uuid.uuid4())),
        ("parameters", {"app": "parsec"}),
        ("target", "other"),
    )

    for field, value in changes:
        with pytest.raises(ProtocolError, match="invalid_signature"):
            verify({**original, field: value}, KEY, CONNECTION_ID)

    with pytest.raises(ProtocolError, match="invalid_signature"):
        verify(original, b"z" * 32, CONNECTION_ID)


@pytest.mark.parametrize("delta", [-91, 91])
def test_signed_frame_freshness_window(delta):
    original = signed_request()
    with pytest.raises(ProtocolError, match="stale_request"):
        verify(original, KEY, CONNECTION_ID, now=original["issued_at"] + delta)


def test_request_timeout_is_tighter_than_signed_frame_window():
    original = signed_request()
    with pytest.raises(ProtocolError, match="stale_request"):
        request(original, target="desktop", now=original["issued_at"] + 31)


def test_connection_identifier_and_target_are_bound():
    original = signed_request()
    with pytest.raises(ProtocolError, match="superseded_connection"):
        verify(original, KEY, "new-connection")
    with pytest.raises(ProtocolError, match="wrong_target"):
        request(original, target="other")


@pytest.mark.parametrize(
    "raw",
    ["[]", '{"x":1,"x":2}', '{"x":NaN}', "bad", "x" * 32769],
)
def test_malformed_payloads_are_rejected(raw):
    with pytest.raises(ProtocolError, match="malformed_message"):
        decode(raw)


@pytest.mark.parametrize(
    "action,values",
    [
        ("shell", {}),
        ("desktop.app.launch", {"app": r"C:\bad.exe"}),
        ("desktop.app.launch", {"app": "parsec", "args": []}),
        ("desktop.app.launch", {"app": "x;shutdown"}),
        ("desktop.app.launch", {"app": "UPPER"}),
        ("desktop.app.launch", {"app": "a" * 65}),
        ("desktop.app.launch", {"app": None}),
        ("desktop.app.list", {"path": "x"}),
        ("desktop.app.launch", {}),
    ],
)
def test_only_allowlisted_symbolic_commands_and_parameters(action, values):
    with pytest.raises(ProtocolError):
        parameters(action, values)


def test_protocol_exposes_only_interactive_application_actions():
    assert set(SCHEMAS) == {
        "desktop.agent.status",
        "desktop.session.status",
        "desktop.app.list",
        "desktop.app.status",
        "desktop.app.launch",
    }
    assert MUTATIONS == {"desktop.app.launch"}


def test_idempotency_duplicate_returns_cached_terminal_result():
    cache = ReplayCache()
    original = signed_request()
    result = {"success": True, "state": "running"}
    cache.put(original, result)

    assert cache.get(original) == result
    retry = {**original, "request_id": str(uuid.uuid4())}
    assert cache.get(retry) == result


@pytest.mark.parametrize("identity", ["request_id", "idempotency_key"])
def test_conflicting_identity_reuse_fails_closed(identity):
    cache = ReplayCache()
    original = signed_request()
    cache.put(original, {"success": True})
    conflict = signed_request(values={"app": "parsec"})
    conflict[identity] = original[identity]

    with pytest.raises(ProtocolError, match="duplicate_request"):
        cache.get(conflict)


def test_replay_cache_is_bounded_and_expires():
    clock = [0]
    cache = ReplayCache(clock=lambda: clock[0], capacity=2)
    original = signed_request()
    cache.put(original, {})
    for _ in range(3):
        cache.put(signed_request(), {})
    assert len(cache.entries) == 4

    clock[0] = 301
    assert cache.get(original) is None
    assert not cache.entries
