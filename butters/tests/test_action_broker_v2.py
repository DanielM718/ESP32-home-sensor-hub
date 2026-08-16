from __future__ import annotations

import json
import os
import struct
import threading

import pytest
from butters.actions.broker import (
    BrokerClient,
    BrokerError,
    BrokerOperation,
    BrokerServer,
    FixedBrokerConfig,
    FixedBrokerOperations,
    _parse_request,
)
from butters.assistant_config import BrokerSettings


class FakeConnection:
    def __init__(self, payload: bytes, uid: int) -> None:
        self.payload = bytearray(payload)
        self.uid = uid
        self.output = bytearray()

    def getsockopt(self, _level, _option, _length):
        return struct.pack("3i", 123, self.uid, 123)

    def recv(self, count: int) -> bytes:
        value = bytes(self.payload[:count])
        del self.payload[:count]
        return value

    def sendall(self, value: bytes) -> None:
        self.output.extend(value)


def _exchange(
    server: BrokerServer, payload: bytes, *, uid: int | None = None
) -> dict[str, object]:
    connection = FakeConnection(payload, os.getuid() if uid is None else uid)
    server.handle(connection)  # type: ignore[arg-type]
    return json.loads(connection.output)


def _request(operation: str, request_id: str = "request_identifier_123") -> bytes:
    return (
        json.dumps(
            {"version": 1, "request_id": request_id, "operation": operation}
        ).encode()
        + b"\n"
    )


def test_broker_accepts_only_expected_peer_enumerated_schema_and_one_use_id() -> None:
    calls: list[str] = []
    server = BrokerServer(
        {
            BrokerOperation.DESKTOP_WAKE: lambda: (
                calls.append("wake") or {"accepted": True}
            )
        },
        expected_uid=os.getuid(),
    )
    ok = _exchange(server, _request("desktop.wake"))
    assert ok["ok"] is True and calls == ["wake"]
    replay = _exchange(server, _request("desktop.wake"))
    assert replay["ok"] is False and replay["error_code"] == "replayed_request"
    unknown = _exchange(server, _request("shell.run", "another_request_id_456"))
    assert unknown["ok"] is False and unknown["error_code"] == "unknown_operation"
    extra = (
        json.dumps(
            {
                "version": 1,
                "request_id": "third_request_identifier",
                "operation": "desktop.wake",
                "command": "rm -rf /",
            }
        ).encode()
        + b"\n"
    )
    malformed = _exchange(server, extra)
    assert malformed["ok"] is False and malformed["error_code"] == "malformed_request"
    assert calls == ["wake"]


def test_broker_rejects_wrong_peer_malformed_types_protocol_and_oversize() -> None:
    wrong_peer = BrokerServer({}, expected_uid=os.getuid() + 1)
    denied = _exchange(wrong_peer, _request("desktop.wake"), uid=os.getuid())
    assert denied["error_code"] == "peer_denied"
    with pytest.raises(BrokerError):
        _parse_request(b'{"version":1}\n', 1)
    with pytest.raises(BrokerError) as protocol:
        _parse_request(
            b'{"version":2,"request_id":"request_identifier_123","operation":"desktop.wake"}',
            1,
        )
    assert protocol.value.code == "protocol_mismatch"
    tiny = BrokerServer({}, expected_uid=os.getuid(), max_message_bytes=128)
    oversized = _exchange(tiny, b"{" + b"x" * 256 + b"}\n")
    assert oversized["error_code"] == "message_too_large"


def test_fixed_operations_use_only_server_configuration_and_explicit_argv(
    tmp_path,
) -> None:
    invocations: list[tuple[list[str], dict[str, object]]] = []

    class Result:
        returncode = 0

    def runner(argv, **kwargs):
        invocations.append((argv, kwargs))
        return Result()

    config = FixedBrokerConfig(
        "fixed-desktop",
        "fixed-user",
        "00:11:22:33:44:55",
        "192.0.2.255",
        tmp_path / "fixed-key",
        enabled_operations=frozenset(
            {BrokerOperation.DESKTOP_WAKE, BrokerOperation.DESKTOP_ENTER_REMOTE}
        ),
    )
    handlers = FixedBrokerOperations(config, runner=runner).handlers()
    assert set(handlers) == {
        BrokerOperation.DESKTOP_WAKE,
        BrokerOperation.DESKTOP_ENTER_REMOTE,
    }
    handlers[BrokerOperation.DESKTOP_WAKE]()
    handlers[BrokerOperation.DESKTOP_ENTER_REMOTE]()
    assert invocations[0][0] == [
        "/usr/bin/wakeonlan",
        "-i",
        "192.0.2.255",
        "00:11:22:33:44:55",
    ]
    assert invocations[1][0][-2:] == [
        "fixed-user@fixed-desktop",
        'schtasks.exe /Run /TN "Enter Remote Mode"',
    ]
    assert all("shell" not in kwargs for _argv, kwargs in invocations)
    assert all(isinstance(argv, list) for argv, _kwargs in invocations)


def test_broker_bounds_malformed_or_excessive_operation_results() -> None:
    malformed = BrokerServer(
        {BrokerOperation.DESKTOP_WAKE: lambda: "not-a-dict"},  # type: ignore[dict-item]
        expected_uid=os.getuid(),
    )
    response = _exchange(malformed, _request("desktop.wake"))
    assert response["error_code"] == "malformed_result"
    excessive = BrokerServer(
        {BrokerOperation.DESKTOP_WAKE: lambda: {"data": "x" * 5000}},
        expected_uid=os.getuid(),
        max_message_bytes=256,
    )
    response = _exchange(excessive, _request("desktop.wake"))
    assert response["error_code"] == "result_too_large"


class ClientConnection:
    def __init__(self, response: bytes = b"", failure: Exception | None = None) -> None:
        self.response = bytearray(response)
        self.failure = failure

    def settimeout(self, _timeout):
        return None

    def connect(self, _path):
        if self.failure:
            raise self.failure

    def sendall(self, _value):
        return None

    def recv(self, count):
        value = bytes(self.response[:count])
        del self.response[:count]
        return value

    def close(self):
        return None


def test_broker_client_fails_closed_on_unavailable_timeout_and_bad_response(
    tmp_path,
) -> None:
    settings = BrokerSettings(enabled=True, socket_path=tmp_path / "broker.sock")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(BrokerError) as stopped:
        BrokerClient(settings).request(
            BrokerOperation.DESKTOP_WAKE,
            request_id="request_identifier_cancelled",
            cancel_event=cancelled,
        )
    assert stopped.value.code == "cancelled"
    unavailable = BrokerClient(
        settings,
        connector=lambda *_args: ClientConnection(failure=OSError("no socket")),
    )
    with pytest.raises(BrokerError) as missing:
        unavailable.request(
            BrokerOperation.DESKTOP_WAKE, request_id="request_identifier_123"
        )
    assert missing.value.code == "broker_unavailable"
    timeout = BrokerClient(
        settings, connector=lambda *_args: ClientConnection(failure=TimeoutError())
    )
    with pytest.raises(BrokerError) as timed_out:
        timeout.request(
            BrokerOperation.DESKTOP_WAKE, request_id="request_identifier_456"
        )
    assert timed_out.value.code == "broker_unavailable"
    malformed = BrokerClient(
        settings, connector=lambda *_args: ClientConnection(b"not-json\n")
    )
    with pytest.raises(BrokerError) as bad:
        malformed.request(
            BrokerOperation.DESKTOP_WAKE, request_id="request_identifier_789"
        )
    assert bad.value.code == "malformed_response"
    wrong_version = {
        "version": 2,
        "request_id": "request_identifier_abc",
        "ok": True,
        "status": {},
        "error_code": None,
    }
    mismatch = BrokerClient(
        settings,
        connector=lambda *_args: ClientConnection(
            json.dumps(wrong_version).encode() + b"\n"
        ),
    )
    with pytest.raises(BrokerError) as protocol:
        mismatch.request(
            BrokerOperation.DESKTOP_WAKE, request_id="request_identifier_abc"
        )
    assert protocol.value.code == "protocol_mismatch"


def test_desktop_ssh_pins_the_host_key_and_refuses_pty_or_forwarding(
    tmp_path,
) -> None:
    """A root credential that can power off a desktop must not trust-on-first-use.

    The broker unit runs with ProtectHome, so an implicit ~/.ssh lookup has no
    known_hosts at all; the pin has to be explicit and provisioned beside the key.
    """

    invocations: list[list[str]] = []

    class Result:
        returncode = 0

    config = FixedBrokerConfig(
        "fixed-desktop",
        "fixed-user",
        "00:11:22:33:44:55",
        "192.0.2.255",
        tmp_path / "credentials" / "desktop_ed25519",
        enabled_operations=frozenset({BrokerOperation.DESKTOP_RESTART}),
    )
    assert config.desktop_known_hosts == tmp_path / "credentials" / "known_hosts"
    handlers = FixedBrokerOperations(
        config,
        runner=lambda argv, **_kwargs: (invocations.append(argv) or Result()),
    ).handlers()
    handlers[BrokerOperation.DESKTOP_RESTART]()
    argv = invocations[0]
    options = {argv[index + 1] for index, item in enumerate(argv) if item == "-o"}
    assert "StrictHostKeyChecking=yes" in options
    assert f"UserKnownHostsFile={config.desktop_known_hosts}" in options
    assert {"BatchMode=yes", "IdentitiesOnly=yes"} <= options
    assert "PreferredAuthentications=publickey" in options
    assert "ClearAllForwardings=yes" in options
    assert "-T" in argv
    assert not any(item.startswith("StrictHostKeyChecking=no") for item in options)
    assert argv[-2:] == ["fixed-user@fixed-desktop", "shutdown.exe /r /t 0"]


def test_broker_dedupe_eviction_drops_the_oldest_request_id() -> None:
    server = BrokerServer(
        {BrokerOperation.DESKTOP_WAKE: lambda: {"accepted": True}},
        expected_uid=os.getuid(),
        dedupe_capacity=2,
    )
    first = "request_identifier_0001"
    for index in range(3):
        assert _exchange(server, _request("desktop.wake", f"request_id_{index:016d}"))[
            "ok"
        ] is True
    assert _exchange(server, _request("desktop.wake", first))["ok"] is True
    # The most recent IDs must still be retained rather than arbitrarily evicted.
    replay = _exchange(server, _request("desktop.wake", first))
    assert replay["ok"] is False and replay["error_code"] == "replayed_request"
    newest = _exchange(server, _request("desktop.wake", "request_id_0000000000000002"))
    assert newest["ok"] is False and newest["error_code"] == "replayed_request"
