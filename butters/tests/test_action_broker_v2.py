from __future__ import annotations

import json
import os
import struct
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

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

BUTTERS_ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    """Deterministic monotonic/sleep pair for the monitor settling loop.

    Substituting both halves keeps the bounded poll instant: sleeping advances
    the clock the loop reads, so a 20s deadline costs no wall time and the
    recorded intervals stay assertable.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeConnection:
    def __init__(self, payload: bytes, uid: int) -> None:
        self.payload = bytearray(payload)
        self.uid = uid
        self.output = bytearray()
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

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
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "fixed-key",
        enabled_operations=frozenset(
            {BrokerOperation.DESKTOP_WAKE, BrokerOperation.DESKTOP_MONITORS_OFF}
        ),
    )
    states = {
        "switch.desktop_gigabyte": "on",
        "switch.desktop_oled": "on",
    }
    ha_requests = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum):
            # http.client returns at most `maximum` bytes and an empty bytes
            # object at EOF. A fake that re-returns the whole body on every
            # call cannot express a body that is still arriving, which is the
            # shape the broker's read deadline exists to bound.
            chunk = self.payload[self.offset : self.offset + maximum]
            self.offset += len(chunk)
            return chunk

    def opener(request, **_kwargs):
        ha_requests.append(request)
        entity = request.full_url.rsplit("/", 1)[-1]
        if request.method == "GET":
            return Response({"entity_id": entity, "state": states[entity]})
        payload = json.loads(request.data)
        assert payload == {
            "entity_id": [
                "switch.desktop_gigabyte",
                "switch.desktop_oled",
            ]
        }
        states.update({entity_id: "off" for entity_id in payload["entity_id"]})
        return Response([])

    handlers = FixedBrokerOperations(
        config,
        runner=runner,
        home_assistant_token="test-token",
        opener=opener,
    ).handlers()
    assert set(handlers) == {
        BrokerOperation.DESKTOP_WAKE,
        BrokerOperation.DESKTOP_MONITORS_OFF,
    }
    handlers[BrokerOperation.DESKTOP_WAKE]()
    monitor_result = handlers[BrokerOperation.DESKTOP_MONITORS_OFF]()
    assert invocations[0][0] == [
        "/usr/bin/wakeonlan",
        "-i",
        "192.168.1.255",
        "34:5A:60:D7:4C:2C",
    ]
    assert monitor_result["accepted"] is True
    assert monitor_result["entities"] == {
        "switch.desktop_gigabyte": "off",
        "switch.desktop_oled": "off",
    }
    assert len([request for request in ha_requests if request.method == "POST"]) == 1
    assert all("shell" not in kwargs for _argv, kwargs in invocations)
    assert all(isinstance(argv, list) for argv, _kwargs in invocations)


def test_monitor_control_reports_already_partial_auth_and_unavailable_states(
    tmp_path,
) -> None:
    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum):
            # http.client returns at most `maximum` bytes and an empty bytes
            # object at EOF. A fake that re-returns the whole body on every
            # call cannot express a body that is still arriving, which is the
            # shape the broker's read deadline exists to bound.
            chunk = self.payload[self.offset : self.offset + maximum]
            self.offset += len(chunk)
            return chunk

    config = FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "windows_remote_mode",
        enabled_operations=frozenset(
            {
                BrokerOperation.DESKTOP_MONITORS_ON,
                BrokerOperation.DESKTOP_MONITORS_OFF,
            }
        ),
    )
    assert FixedBrokerOperations.MONITOR_ENTITIES == (
        "switch.desktop_gigabyte",
        "switch.desktop_oled",
    )
    assert not any(
        entity.endswith(("_led", "_auto_off_enabled", "_auto_update_enabled"))
        for entity in FixedBrokerOperations.MONITOR_ENTITIES
    )
    states = {
        "switch.desktop_gigabyte": "on",
        "switch.desktop_oled": "on",
    }

    def partial_opener(request, **_kwargs):
        entity = request.full_url.rsplit("/", 1)[-1]
        if request.method == "GET":
            return Response({"state": states[entity]})
        states["switch.desktop_gigabyte"] = "off"
        states["switch.desktop_oled"] = "unavailable"
        return Response([])

    # The unavailable outlet never reaches "off", so the settling loop runs to
    # its deadline; the fake clock keeps that bounded wait free of wall time.
    clock = FakeClock()
    operations = FixedBrokerOperations(
        config,
        home_assistant_token="token",
        opener=partial_opener,
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )
    already = operations.desktop_monitors_on()
    partial = operations.desktop_monitors_off()
    assert already["accepted"] is True and already["already_in_state"] is True
    assert partial["accepted"] is False and partial["partial"] is True
    assert set(partial["entities"]) == {
        "switch.desktop_gigabyte",
        "switch.desktop_oled",
    }

    def denied(*_args, **_kwargs):
        raise HTTPError("http://127.0.0.1", 401, "denied", {}, None)

    with pytest.raises(BrokerError) as auth:
        FixedBrokerOperations(
            config, home_assistant_token="token", opener=denied
        ).desktop_monitors_on()
    assert auth.value.code == "home_assistant_auth_failed"

    with pytest.raises(BrokerError) as unconfigured:
        FixedBrokerOperations(config).desktop_monitors_on()
    assert unconfigured.value.code == "home_assistant_unconfigured"


def test_active_desktop_code_and_configuration_have_no_retired_direct_link_path() -> (
    None
):
    retired = (
        "169.254.255.255",
        "169.254.227.84",
        "Enter Remote Mode",
        "Restore Local Mode",
        "ssh_begin_remote",
        "ssh_restore_local",
    )
    paths = [
        *sorted((BUTTERS_ROOT / "src").rglob("*.py")),
        *sorted((BUTTERS_ROOT / "config").glob("*.toml")),
    ]
    active = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for obsolete in retired:
        assert obsolete not in active


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
        self.timeouts: list[float] = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

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


def test_broker_client_clips_every_socket_phase_to_one_absolute_deadline(
    tmp_path,
) -> None:
    request_id = "request_identifier_deadline"
    payload = {
        "version": 1,
        "request_id": request_id,
        "ok": True,
        "status": {"accepted": True},
        "error_code": None,
    }
    connection = ClientConnection(json.dumps(payload).encode() + b"\n")
    readings = iter((0.0, 0.2, 0.6, 0.8))
    client = BrokerClient(
        BrokerSettings(enabled=True, socket_path=tmp_path / "broker.sock"),
        connector=lambda *_args: connection,
        monotonic=lambda: next(readings),
    )

    result = client.request(
        BrokerOperation.DESKTOP_WAKE,
        request_id=request_id,
        timeout_seconds=1.0,
    )

    assert result.ok is True
    assert connection.timeouts == pytest.approx((0.8, 0.4, 0.2))


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
        stdout = json.dumps(
            {"accepted": True, "transition": "restart", "scheduled": True}
        )

    config = FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "credentials" / "windows_remote_mode",
        enabled_operations=frozenset({BrokerOperation.DESKTOP_RESTART}),
    )
    assert config.desktop_known_hosts == tmp_path / "credentials" / "known_hosts"
    handlers = FixedBrokerOperations(
        config,
        runner=lambda argv, **_kwargs: invocations.append(argv) or Result(),
    ).handlers()
    handlers[BrokerOperation.DESKTOP_RESTART]()
    argv = invocations[0]
    options = {argv[index + 1] for index, item in enumerate(argv) if item == "-o"}
    assert "StrictHostKeyChecking=yes" in options
    assert f"UserKnownHostsFile={config.desktop_known_hosts}" in options
    assert {"BatchMode=yes", "IdentitiesOnly=yes"} <= options
    assert "PreferredAuthentications=publickey" in options
    assert "ClearAllForwardings=yes" in options
    assert "UpdateHostKeys=no" in options
    assert "-T" in argv
    assert not any(item.startswith("StrictHostKeyChecking=no") for item in options)
    assert argv[-2:] == [
        "Daniel@192.168.1.209",
        (
            "powershell.exe -NoLogo -NoProfile -NonInteractive "
            "-ExecutionPolicy Bypass -File "
            "C:\\ProgramData\\Butters\\desktop-control.ps1 -Operation Restart"
        ),
    ]
    assert argv[argv.index("-i") + 1].endswith("windows_remote_mode")


def test_broker_dedupe_eviction_drops_the_oldest_request_id() -> None:
    server = BrokerServer(
        {BrokerOperation.DESKTOP_WAKE: lambda: {"accepted": True}},
        expected_uid=os.getuid(),
        dedupe_capacity=2,
    )
    first = "request_identifier_0001"
    for index in range(3):
        assert (
            _exchange(server, _request("desktop.wake", f"request_id_{index:016d}"))[
                "ok"
            ]
            is True
        )
    assert _exchange(server, _request("desktop.wake", first))["ok"] is True
    # The most recent IDs must still be retained rather than arbitrarily evicted.
    replay = _exchange(server, _request("desktop.wake", first))
    assert replay["ok"] is False and replay["error_code"] == "replayed_request"
    newest = _exchange(server, _request("desktop.wake", "request_id_0000000000000002"))
    assert newest["ok"] is False and newest["error_code"] == "replayed_request"


class _MonitorHarness:
    """Fake Home Assistant whose two outlets settle after a chosen poll count.

    settle_after maps an entity to the number of settling sleeps that must elapse
    after the service call before it reports the requested state; 0 means it is
    already there on the first post-call poll, and None means it never settles.
    Polls are counted per entity, so asynchronous convergence is expressible
    without any real time passing.
    """

    def __init__(
        self,
        initial: str,
        settle_after: dict[str, int | None],
        *,
        fail_after_reads: int | None = None,
        failure: Exception | None = None,
        unavailable_after: dict[str, int] | None = None,
    ) -> None:
        self.states = {
            entity: initial for entity in FixedBrokerOperations.MONITOR_ENTITIES
        }
        self.settle_after = settle_after
        self.unavailable_after = unavailable_after or {}
        self.fail_after_reads = fail_after_reads
        self.failure = failure
        self.desired: str | None = None
        self.reads: dict[str, int] = dict.fromkeys(self.states, 0)
        self.polls: dict[str, int] = dict.fromkeys(self.states, 0)
        self.total_reads = 0
        self.posts: list[dict[str, object]] = []
        self.get_paths: list[str] = []

    class Response:
        status = 200

        def __init__(self, payload: object) -> None:
            self.payload = json.dumps(payload).encode()
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum: int) -> bytes:
            chunk = self.payload[self.offset : self.offset + maximum]
            self.offset += len(chunk)
            return chunk

    def opener(self, request, **_kwargs):
        if request.method == "POST":
            path = request.full_url.split("/api/", 1)[-1]
            body = json.loads(request.data)
            self.posts.append({"path": path, "body": body})
            self.desired = path.rsplit("turn_", 1)[-1]
            return self.Response([])
        entity = request.full_url.rsplit("/", 1)[-1]
        self.get_paths.append(request.full_url.split("/api/", 1)[-1])
        self.total_reads += 1
        if (
            self.fail_after_reads is not None
            and self.total_reads > self.fail_after_reads
            and self.failure is not None
        ):
            raise self.failure
        self.reads[entity] = self.reads.get(entity, 0) + 1
        if self.desired is not None:
            # Nth post-call poll for this entity (the pre-call read is excluded).
            self.polls[entity] = self.polls.get(entity, 0) + 1
            limit = self.unavailable_after.get(entity)
            if limit is not None and self.polls[entity] > limit:
                return self.Response({"state": "unavailable"})
            needed = self.settle_after.get(entity)
            if needed is not None and self.polls[entity] > needed:
                self.states[entity] = self.desired
        return self.Response({"state": self.states[entity]})


def _monitor_config(tmp_path) -> FixedBrokerConfig:
    return FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "windows_remote_mode",
        enabled_operations=frozenset(
            {
                BrokerOperation.DESKTOP_MONITORS_ON,
                BrokerOperation.DESKTOP_MONITORS_OFF,
            }
        ),
    )


def _monitor_operations(tmp_path, harness) -> tuple[FixedBrokerOperations, FakeClock]:
    clock = FakeClock()
    return (
        FixedBrokerOperations(
            _monitor_config(tmp_path),
            home_assistant_token="token",
            opener=harness.opener,
            sleeper=clock.sleep,
            monotonic=clock.monotonic,
        ),
        clock,
    )


def test_monitor_settling_accepts_when_both_outlets_converge_immediately(
    tmp_path,
) -> None:
    harness = _MonitorHarness(
        "on", dict.fromkeys(FixedBrokerOperations.MONITOR_ENTITIES, 0)
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is True
    assert result["partial"] is False
    assert result["already_in_state"] is False
    assert result["entities"] == dict.fromkeys(
        FixedBrokerOperations.MONITOR_ENTITIES, "off"
    )
    # Converged on the first post-call read, so the loop never slept.
    assert clock.slept == []
    assert len(harness.posts) == 1


def test_monitor_settling_accepts_when_one_outlet_lags_several_polls(tmp_path) -> None:
    harness = _MonitorHarness(
        "on",
        {
            "switch.desktop_gigabyte": 4,
            "switch.desktop_oled": 0,
        },
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is True
    assert result["partial"] is False
    assert result["entities"]["switch.desktop_gigabyte"] == "off"
    assert result["entities"]["switch.desktop_oled"] == "off"
    # Polled rather than failing, and every wait used the fixed interval.
    assert len(clock.slept) == 4
    assert set(clock.slept) == {0.5}
    assert len(harness.posts) == 1


def test_monitor_settling_handles_asynchronous_convergence(tmp_path) -> None:
    # Mirrors production: the OLED settles quickly, the Gigabyte lags well behind.
    harness = _MonitorHarness(
        "off",
        {
            "switch.desktop_gigabyte": 30,
            "switch.desktop_oled": 2,
        },
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_on()

    assert result["accepted"] is True
    assert result["partial"] is False
    assert result["entities"] == dict.fromkeys(
        FixedBrokerOperations.MONITOR_ENTITIES, "on"
    )
    # ~15s of skew is absorbed inside the 20s deadline.
    assert clock.now == pytest.approx(15.0)
    assert clock.now < 20.0


def test_monitor_settling_accepts_convergence_just_before_the_deadline(
    tmp_path,
) -> None:
    # 39 settling sleeps of 0.5s puts convergence at t=19.5s, inside the deadline.
    harness = _MonitorHarness(
        "on",
        {
            "switch.desktop_gigabyte": 39,
            "switch.desktop_oled": 0,
        },
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is True
    assert result["partial"] is False
    assert clock.now == pytest.approx(19.5)
    assert clock.now < 20.0


def test_monitor_settling_reports_partial_when_one_outlet_never_converges(
    tmp_path,
) -> None:
    harness = _MonitorHarness(
        "on",
        {
            "switch.desktop_gigabyte": None,
            "switch.desktop_oled": 0,
        },
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is False
    assert result["partial"] is True
    assert result["entities"]["switch.desktop_oled"] == "off"
    assert result["entities"]["switch.desktop_gigabyte"] == "on"
    # Bounded: it stopped at the deadline instead of blocking.
    assert clock.now == pytest.approx(20.0)
    assert len(harness.posts) == 1


def test_monitor_settling_fails_closed_when_neither_outlet_converges(tmp_path) -> None:
    harness = _MonitorHarness(
        "on", dict.fromkeys(FixedBrokerOperations.MONITOR_ENTITIES, None)
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is False
    assert result["partial"] is False
    assert result["entities"] == dict.fromkeys(
        FixedBrokerOperations.MONITOR_ENTITIES, "on"
    )
    assert clock.now == pytest.approx(20.0)


def test_monitor_settling_treats_becoming_unavailable_as_not_converged(
    tmp_path,
) -> None:
    harness = _MonitorHarness(
        "on",
        {
            "switch.desktop_gigabyte": 0,
            "switch.desktop_oled": 0,
        },
        unavailable_after={"switch.desktop_oled": 0},
    )
    operations, clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()

    assert result["accepted"] is False
    assert result["partial"] is True
    assert result["entities"]["switch.desktop_oled"] == "unavailable"
    assert result["entities"]["switch.desktop_gigabyte"] == "off"
    assert clock.now == pytest.approx(20.0)


def test_monitor_settling_propagates_read_failures_during_verification(
    tmp_path,
) -> None:
    auth = _MonitorHarness(
        "on",
        {"switch.desktop_gigabyte": None, "switch.desktop_oled": 0},
        fail_after_reads=3,
        failure=HTTPError("http://127.0.0.1", 401, "denied", {}, None),
    )
    operations, _clock = _monitor_operations(tmp_path, auth)
    with pytest.raises(BrokerError) as denied:
        operations.desktop_monitors_off()
    assert denied.value.code == "home_assistant_auth_failed"

    network = _MonitorHarness(
        "on",
        {"switch.desktop_gigabyte": None, "switch.desktop_oled": 0},
        fail_after_reads=3,
        failure=URLError("unreachable"),
    )
    operations, _clock = _monitor_operations(tmp_path, network)
    with pytest.raises(BrokerError) as unavailable:
        operations.desktop_monitors_off()
    assert unavailable.value.code == "home_assistant_unavailable"


def test_monitor_settling_never_widens_the_fixed_entity_or_service_surface(
    tmp_path,
) -> None:
    from butters.actions import broker as broker_module

    assert FixedBrokerOperations.MONITOR_ENTITIES == (
        "switch.desktop_gigabyte",
        "switch.desktop_oled",
    )
    harness = _MonitorHarness(
        "on",
        {
            "switch.desktop_gigabyte": 3,
            "switch.desktop_oled": 1,
        },
    )
    operations, _clock = _monitor_operations(tmp_path, harness)

    result = operations.desktop_monitors_off()
    assert result["accepted"] is True

    # Exactly one service call, to the fixed domain/service, naming only the two
    # approved entities.
    assert len(harness.posts) == 1
    assert harness.posts[0]["path"] == "services/switch/turn_off"
    assert harness.posts[0]["body"] == {
        "entity_id": [
            "switch.desktop_gigabyte",
            "switch.desktop_oled",
        ]
    }
    # Every polled read targeted only the two approved entities.
    assert set(harness.get_paths) == {
        "states/switch.desktop_gigabyte",
        "states/switch.desktop_oled",
    }
    # The settling bounds are broker-local constants, not caller or config input.
    assert broker_module.MONITOR_SETTLE_DEADLINE_SECONDS == 20.0
    assert broker_module.MONITOR_SETTLE_POLL_SECONDS == 0.5
    assert broker_module.MONITOR_SETTLE_DEADLINE_SECONDS > 15.0
    assert (
        broker_module.MONITOR_SETTLE_DEADLINE_SECONDS
        < BrokerSettings().request_timeout_seconds
    )


# --- Home Assistant is a separate trust domain ----------------------------


def _monitor_config(tmp_path) -> FixedBrokerConfig:
    return FixedBrokerConfig(
        "192.168.1.209",
        "Daniel",
        "34:5A:60:D7:4C:2C",
        "192.168.1.255",
        tmp_path / "fixed-key",
        enabled_operations=frozenset({BrokerOperation.DESKTOP_MONITORS_OFF}),
        # Never the real 127.0.0.1:8123: a fake handler that fails to displace
        # urllib's HTTPHandler would otherwise reach the live Home Assistant on
        # the development host instead of failing the test.
        home_assistant_url="http://127.0.0.1:65535",
    )


def test_a_home_assistant_redirect_never_carries_the_token_off_host(tmp_path) -> None:
    """Regression: urlopen would re-send the Bearer token wherever a 302 points.

    urllib copies every request header except content-length/content-type onto
    a redirect target, on any host. Home Assistant loads third-party
    integrations and is a distinct trust domain, so a single 302 on the first
    GET of a monitors operation would have exfiltrated the long-lived token
    from this root process, while the operation still reported success.
    """

    import io
    import urllib.request
    from email.message import Message
    from urllib.response import addinfourl

    def _response(body, headers, url, code, reason):
        # HTTPErrorProcessor reads .msg off the response; addinfourl delegates
        # unknown attributes to the file object, which has none.
        response = addinfourl(io.BytesIO(body), headers, url, code)
        response.msg = reason
        return response

    seen: list[tuple[str, str | None]] = []

    # Subclassing HTTPHandler is what makes build_opener *replace* urllib's own
    # handler. A plain BaseHandler is merely appended, and the real one wins.
    class Redirector(urllib.request.HTTPHandler):
        def http_open(self, request):
            seen.append((request.full_url, request.get_header("Authorization")))
            headers = Message()
            if "192.0.2." in request.full_url:
                headers["Content-Type"] = "application/json"
                return _response(b"{}", headers, request.full_url, 200, "OK")
            headers["Location"] = "http://192.0.2.10/collect"
            return _response(b"", headers, request.full_url, 302, "Found")

    from butters.actions.broker import _NoRedirect

    opener = urllib.request.build_opener(_NoRedirect, Redirector).open
    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="LONG_LIVED_TOKEN",
        opener=opener,
    )

    with pytest.raises(BrokerError) as failure:
        operations.desktop_monitors_off()

    assert failure.value.code == "home_assistant_failed"
    assert seen, "the fake handler never ran; urllib would have used the network"
    off_host = [item for item in seen if "127.0.0.1" not in item[0]]
    assert off_host == [], f"token was sent off-host: {off_host}"


def test_the_redirect_handler_refuses_rather_than_rewrites() -> None:
    from butters.actions.broker import _NoRedirect

    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere") is None


def test_a_dripping_home_assistant_response_cannot_hold_the_serial_broker(
    tmp_path,
) -> None:
    """Regression: urlopen's timeout is per socket operation, not a deadline.

    A peer that sends one byte just inside every timeout window held a single
    read open indefinitely. The broker serves connections serially with
    Restart=no and no watchdog, so that one read blocked every other privileged
    operation until an operator intervened.
    """

    clock = FakeClock()
    reads = {"count": 0}

    class Dripping:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            reads["count"] += 1
            # Just inside the 5s socket timeout, so no read ever raises.
            clock.now += 4.0
            return b"{"

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="token",
        opener=lambda _request, **_kwargs: Dripping(),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    with pytest.raises(BrokerError) as failure:
        operations.desktop_monitors_off()

    assert failure.value.code == "home_assistant_unavailable"
    # Bounded by HOME_ASSISTANT_DEADLINE_SECONDS / 4s per read, not by the
    # 64 KiB cap, which one-byte reads would take 65537 reads to reach.
    assert reads["count"] <= 8


def test_a_whole_home_assistant_body_still_arrives_in_chunks(tmp_path) -> None:
    """The deadline must not truncate a large but timely response."""

    payload = json.dumps({"state": "off", "filler": "x" * 40000}).encode()

    class Chunked:
        status = 200

        def __init__(self) -> None:
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum):
            chunk = payload[self.offset : self.offset + maximum]
            self.offset += len(chunk)
            return chunk

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="token",
        opener=lambda _request, **_kwargs: Chunked(),
    )

    assert operations._monitor_states() == {
        "switch.desktop_gigabyte": "off",
        "switch.desktop_oled": "off",
    }


def test_an_unrenderable_status_still_answers_and_keeps_the_outcome() -> None:
    """Encoding sits after handle()'s except clauses.

    A status a handler made unserializable would otherwise escape as an
    unhandled exception after the operation had already run: no reply, no audit
    line, and a broker process that exits. The decision must survive, and a
    mutation that succeeded must never be reported as failed, because that
    invites the caller to retry it.
    """

    import socket as socket_module

    server = BrokerServer(
        {BrokerOperation.DESKTOP_WAKE: lambda: {"accepted": True, "bad": object()}},
        expected_uid=os.getuid(),
    )
    left, right = socket_module.socketpair(socket_module.AF_UNIX)
    request = json.dumps(
        {
            "version": 1,
            "request_id": "unrenderable-status-0001",
            "operation": "desktop.wake",
        }
    ).encode() + b"\n"
    try:
        right.sendall(request)
        server.handle(left)
        reply = json.loads(right.recv(8192).decode().strip())
    finally:
        left.close()
        right.close()

    assert reply["ok"] is True
    assert reply["status"] == {}
    assert reply["request_id"] == "unrenderable-status-0001"


def test_a_default_urllib_opener_would_have_leaked_the_token(tmp_path) -> None:
    """Pins why the no-redirect opener has to exist.

    If a future change reverts the broker to urlopen, or urllib ever starts
    stripping Authorization across hosts, exactly one of these two assertions
    changes and the guard's justification is re-examined rather than silently
    dropped.
    """

    import io
    import urllib.request
    from email.message import Message
    from urllib.response import addinfourl

    seen: list[tuple[str, str | None]] = []

    class Redirector(urllib.request.HTTPHandler):
        def http_open(self, request):
            seen.append((request.full_url, request.get_header("Authorization")))
            headers = Message()
            if "192.0.2." in request.full_url:
                headers["Content-Type"] = "application/json"
                response = addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 200)
                response.msg = "OK"
                return response
            headers["Location"] = "http://192.0.2.10/collect"
            response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
            response.msg = "Found"
            return response

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="LONG_LIVED_TOKEN",
        opener=urllib.request.build_opener(Redirector).open,
    )
    operations._monitor_states()

    off_host = [item for item in seen if "192.0.2." in item[0]]
    assert off_host, "the redirect was not followed; this test no longer proves anything"
    assert off_host[0][1] == "Bearer LONG_LIVED_TOKEN"


# --- the whole Home Assistant exchange is bounded, not just the body -------


def test_a_slow_response_header_cannot_hold_the_serial_broker(tmp_path) -> None:
    """Regression: urlopen's timeout is per socket operation, not a deadline.

    A peer dripping one byte of the status line or headers just inside every
    window held urlopen open for a measured 106 seconds against a 5s timeout,
    and indefinitely if it never stops. _read_bounded covers only the body,
    which is reached after that phase. The broker answers connections serially,
    so one such call blocks every other privileged operation.
    """

    import threading

    release = threading.Event()

    class NeverReturnsHeaders:
        status = 200

        def __enter__(self):
            # Stands in for the status-line/header phase inside urlopen, which
            # completes before any body read and is not covered by _read_bounded.
            release.wait(30)
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return b"{}"

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="token",
        opener=lambda _request, **_kwargs: NeverReturnsHeaders(),
        home_assistant_deadline_seconds=0.2,
    )

    started = time.monotonic()
    try:
        with pytest.raises(BrokerError) as failure:
            operations.desktop_monitors_off()
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert failure.value.code == "home_assistant_unavailable"
    assert elapsed < 5.0, f"the broker was held for {elapsed:.1f}s"


def test_a_wedged_home_assistant_fails_later_requests_immediately(
    tmp_path,
) -> None:
    """A stuck exchange must not queue every later request behind it.

    Abandoning the worker is what keeps the broker responsive; refusing to start
    a second one is what stops an abandoned thread accumulating per request.
    """

    import threading

    release = threading.Event()
    starts = []

    class Hanging:
        status = 200

        def __enter__(self):
            starts.append(1)
            release.wait(30)
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _maximum):
            return b"{}"

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="token",
        opener=lambda _request, **_kwargs: Hanging(),
        home_assistant_deadline_seconds=0.2,
    )

    try:
        with pytest.raises(BrokerError):
            operations.desktop_monitors_off()
        second = time.monotonic()
        with pytest.raises(BrokerError) as failure:
            operations.desktop_monitors_off()
        immediate = time.monotonic() - second
    finally:
        release.set()

    assert failure.value.code == "home_assistant_unavailable"
    assert immediate < 0.1, f"the second request waited {immediate:.2f}s"
    assert len(starts) == 1, "a second exchange was started behind a stuck one"


def test_a_healthy_exchange_is_unaffected_by_the_supervision(tmp_path) -> None:
    """The bound must not close the path it guards."""

    class Fine:
        status = 200

        def __init__(self) -> None:
            self.payload = json.dumps({"state": "off"}).encode()
            self.offset = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum):
            chunk = self.payload[self.offset : self.offset + maximum]
            self.offset += len(chunk)
            return chunk

    operations = FixedBrokerOperations(
        _monitor_config(tmp_path),
        home_assistant_token="token",
        opener=lambda _request, **_kwargs: Fine(),
    )

    assert operations._monitor_states() == {
        "switch.desktop_gigabyte": "off",
        "switch.desktop_oled": "off",
    }
    # Still usable after a completed exchange: the worker is reused, not retired.
    assert operations._monitor_states()["switch.desktop_oled"] == "off"
