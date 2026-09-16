import time
import uuid

import pytest
from butters_nas_agent.protocol import (
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


def _request(action="nas.system.status", values=None):
    connection = "c" * 64
    return {
        **envelope("request", connection),
        "request_id": str(uuid.uuid4()),
        "action": action,
        "target": "nas-primary",
        "parameters": {} if values is None else values,
        "idempotency_key": str(uuid.uuid4()),
        "timeout_seconds": 10,
    }


def test_schema_is_exact_and_shutdown_has_zero_parameters():
    assert set(SCHEMAS) == {
        "nas.agent.status",
        "nas.system.status",
        "nas.jellyfin.status",
        "nas.system.shutdown",
    }
    assert parameters("nas.system.shutdown", {}) == {}
    with pytest.raises(ProtocolError, match="invalid_parameter"):
        parameters("nas.system.shutdown", {"delay": 1})


def test_unknown_action_and_non_dict_are_rejected():
    for action, value in (("shell", {}), ("nas.system.status", [])):
        with pytest.raises(ProtocolError):
            parameters(action, value)


def test_canonical_signatures_are_order_independent_and_verified():
    key = b"k" * 32
    frame = envelope("heartbeat", "connection", seq=1)
    signed = sign(frame, key)
    assert canonical(signed) == canonical(dict(reversed(list(signed.items()))))
    assert verify(signed, key, "connection") is signed


def test_bad_signature_wrong_connection_and_stale_time_fail_closed():
    key = b"k" * 32
    signed = sign(envelope("heartbeat", "connection", seq=1), key)
    for changed, code in (
        ({**signed, "sig": "0" * 64}, "invalid_signature"),
        (signed, "superseded_connection"),
        ({**signed, "issued_at": time.time() - 91}, "stale_request"),
    ):
        with pytest.raises(ProtocolError, match=code):
            verify(
                changed,
                key,
                "wrong" if code == "superseded_connection" else "connection",
            )


def test_duplicate_json_keys_and_nonfinite_values_are_rejected():
    for raw in ('{"x":1,"x":2}', '{"x":NaN}', "[]"):
        with pytest.raises(ProtocolError, match="malformed_message"):
            decode(raw)


def test_request_requires_exact_target_uuid_timeout_and_fields():
    frame = _request()
    request(sign(frame, b"k" * 32), target="nas-primary")
    for changed in (
        {**frame, "target": "desktop"},
        {**frame, "request_id": "not-a-uuid"},
        {**frame, "timeout_seconds": 31},
        {**frame, "method": "system.shutdown"},
    ):
        with pytest.raises(ProtocolError):
            request(sign(changed, b"k" * 32), target="nas-primary")


def test_request_timestamp_must_fit_its_tighter_timeout():
    frame = _request()
    frame["issued_at"] = time.time() - 11
    with pytest.raises(ProtocolError, match="stale_request"):
        request(sign(frame, b"k" * 32), target="nas-primary")


def test_replay_cache_returns_idempotent_result_without_reexecution():
    cache = ReplayCache()
    frame = _request("nas.system.shutdown")
    cache.put(frame, {"success": True, "accepted": True})
    assert cache.get(dict(frame)) == {"success": True, "accepted": True}


def test_reused_idempotency_identity_for_different_work_is_rejected():
    cache = ReplayCache()
    first = _request("nas.system.status")
    cache.put(first, {"success": True})
    second = _request("nas.jellyfin.status")
    second["idempotency_key"] = first["idempotency_key"]
    with pytest.raises(ProtocolError, match="duplicate_request"):
        cache.get(second)
