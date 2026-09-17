import json
import logging
import threading
from pathlib import Path
from typing import ClassVar

import pytest
from butters_nas_agent.backends import JellyfinBackend, TrueNasRpc
from butters_nas_agent.config import AgentConfig
from butters_nas_agent.protocol import ProtocolError


def _config(**changes):
    values = {
        "url": "wss://butters:8443/nas-agent/v1/session",
        "agent_id": "nas-primary",
        "spki_sha256": "a" * 64,
        "truenas_url": "wss://truenas.local/api/current",
        "truenas_username": "status_agent",
        "truenas_shutdown_username": None,
        "truenas_spki_sha256": "b" * 64,
        "jellyfin_url": "http://192.168.1.240:8096",
        "jellyfin_health_path": "/health",
        "jellyfin_session_monitoring_enabled": False,
        "local_networks": ("192.168.1.0/24",),
        "remote_networks": ("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
        "network_monitoring_enabled": False,
        "physical_interface": "eno1",
        "tailscale_metrics_url": "http://100.100.100.100/metrics",
        "sample_interval_seconds": 5.0,
        "smoothing_alpha": 0.35,
        "maximum_sample_interval_seconds": 30.0,
        "effective_capacity_mbps": 30.0,
        "safe_streaming_budget_mbps": 24.0,
        "reserve_mbps": 6.0,
        "minimum_stream_mbps": 3.0,
        "maximum_stream_mbps": 24.0,
        "stream_stability_seconds": 15.0,
        "minimum_change_mbps": 1.0,
        "policy_cooldown_seconds": 30.0,
        "policy_mode": "observe",
        "timeout_seconds": 5.0,
        "heartbeat_seconds": 15.0,
        "shutdown_enabled": False,
        "health_file": Path("/run/health"),
    }
    values.update(changes)
    return AgentConfig(**values)


class RpcSocket:
    def __init__(self, shutdown_result=None, error_method=None):
        self.sent = []
        self.shutdown_result = shutdown_result
        self.error_method = error_method
        self.socket = self
        self.transport_error_method = None
        self.close_calls = 0

    def getpeercert(self, *, binary_form):
        assert binary_form is True
        return b"certificate"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def close(self):
        self.close_calls += 1

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout):
        call = self.sent[-1]
        method = call["method"]
        if method == self.transport_error_method:
            raise TimeoutError
        if method == self.error_method:
            return json.dumps(
                {"jsonrpc": "2.0", "id": call["id"], "error": {"code": -1}}
            )
        values = {
            "auth.login_ex": {"response_type": "SUCCESS", "user_info": None},
            "system.state": "READY",
            "system.version_short": "25.10.5",
            "system.info": {"hostname": "truenas", "uptime_seconds": 123.0},
            "system.shutdown": self.shutdown_result,
        }
        return json.dumps(
            {"jsonrpc": "2.0", "id": call["id"], "result": values[method]}
        )


def _rpc(config, socket, shutdown_key=None):
    rpc = TrueNasRpc(
        config,
        "read-key",
        shutdown_key,
        connector=lambda *a, **k: socket,
        pin_verifier=lambda *_args: None,
    )
    rpc._context = lambda: object()
    return rpc


def test_status_uses_only_fixed_methods_and_projects_safe_fields():
    socket = RpcSocket()
    result = _rpc(_config(), socket).status()
    assert result == {
        "reachable": True,
        "hostname": "truenas",
        "version": "25.10.5",
        "uptime_seconds": 123.0,
        "system_state": "online",
    }
    assert [item["method"] for item in socket.sent] == [
        "auth.login_ex",
        "system.state",
        "system.version_short",
        "system.info",
    ]
    assert socket.sent[0]["params"][0]["login_options"] == {"user_info": False}
    assert socket.sent[0]["params"][0]["username"] == "status_agent"


def test_status_reuses_only_a_recent_successful_snapshot():
    socket = RpcSocket()
    clock = [10.0]
    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=lambda *a, **k: socket,
        pin_verifier=lambda *_args: None,
        monotonic=lambda: clock[0],
    )
    rpc._context = lambda: object()

    first = rpc.status()
    clock[0] = 14.9
    second = rpc.status()
    assert second == first
    assert second is not first
    assert len(socket.sent) == 4

    clock[0] = 15.1
    assert rpc.status() == first
    assert len(socket.sent) == 7
    assert [item["id"] for item in socket.sent] == list(range(1, 8))


def test_status_reuses_one_authenticated_session_after_cache_expiry():
    socket = RpcSocket()
    clock = [10.0]
    connections = 0

    def connector(*_args, **_kwargs):
        nonlocal connections
        connections += 1
        return socket

    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=connector,
        pin_verifier=lambda *_args: None,
        monotonic=lambda: clock[0],
    )
    rpc._context = lambda: object()
    rpc.status()
    clock[0] += 6
    rpc.status()
    assert connections == 1
    assert [item["method"] for item in socket.sent] == [
        "auth.login_ex",
        "system.state",
        "system.version_short",
        "system.info",
        "system.state",
        "system.version_short",
        "system.info",
    ]


def test_stale_persistent_session_is_invalidated_and_reauthenticated_once():
    first = RpcSocket()
    second = RpcSocket()
    sockets = [first, second]
    clock = [10.0]
    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=lambda *_args, **_kwargs: sockets.pop(0),
        pin_verifier=lambda *_args: None,
        monotonic=lambda: clock[0],
    )
    rpc._context = lambda: object()
    rpc.status()
    clock[0] += 6
    first.transport_error_method = "system.state"
    result = rpc.status()
    assert result["system_state"] == "online"
    assert first.close_calls == 1
    assert [item["method"] for item in second.sent] == [
        "auth.login_ex",
        "system.state",
        "system.version_short",
        "system.info",
    ]
    assert sockets == []


def test_timed_out_new_session_is_not_reused_by_the_next_request():
    first = RpcSocket()
    first.transport_error_method = "system.state"
    second = RpcSocket()
    sockets = [first, second]
    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=lambda *_args, **_kwargs: sockets.pop(0),
        pin_verifier=lambda *_args: None,
    )
    rpc._context = lambda: object()
    with pytest.raises(ProtocolError, match="truenas_unavailable"):
        rpc.status()
    assert first.close_calls == 1
    assert rpc.status()["system_state"] == "online"
    assert [item["method"] for item in second.sent] == [
        "auth.login_ex",
        "system.state",
        "system.version_short",
        "system.info",
    ]
    assert sockets == []


def test_heartbeat_and_explicit_status_overlap_share_one_backend_call():
    entered = threading.Event()
    release = threading.Event()

    class BlockingSocket(RpcSocket):
        blocked = False

        def recv(self, timeout):
            if self.sent[-1]["method"] == "system.state" and not self.blocked:
                self.blocked = True
                entered.set()
                assert release.wait(timeout=1)
            return super().recv(timeout)

    socket = BlockingSocket()
    rpc = _rpc(_config(), socket)
    results = []
    first = threading.Thread(target=lambda: results.append(rpc.status()))
    second = threading.Thread(target=lambda: results.append(rpc.status()))
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)
    assert len(results) == 2
    assert results[0] == results[1]
    assert len(socket.sent) == 4


def test_timing_logs_are_bounded_and_never_contain_key_material(caplog):
    socket = RpcSocket()
    with caplog.at_level(logging.INFO, logger="butters_nas_agent"):
        _rpc(_config(), socket).status()
    messages = "\n".join(record.message for record in caplog.records)
    assert "truenas_connect" in messages
    assert "truenas_spki" in messages
    assert "truenas_authentication" in messages
    assert "truenas_rpc" in messages
    assert "read-key" not in messages


def test_one_end_to_end_deadline_bounds_connect_authentication_and_methods():
    clock = [0.0]
    socket = RpcSocket()
    receive_budgets = []
    original_recv = socket.recv

    def timed_recv(timeout):
        receive_budgets.append(timeout)
        clock[0] += 2.0
        return original_recv(timeout)

    socket.recv = timed_recv
    connect_budgets = []

    def connector(*_args, **kwargs):
        connect_budgets.append(kwargs["open_timeout"])
        clock[0] += 2.0
        return socket

    rpc = TrueNasRpc(
        _config(timeout_seconds=10),
        "read-key",
        connector=connector,
        pin_verifier=lambda *_args: None,
        monotonic=lambda: clock[0],
    )
    rpc._context = lambda: object()
    assert rpc.status()["system_state"] == "online"
    assert connect_budgets == [10.0]
    assert receive_budgets == [8.0, 6.0, 4.0, 2.0]
    assert clock[0] == 10.0


def test_failed_status_is_not_cached():
    sockets = [RpcSocket(error_method="system.state"), RpcSocket()]
    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=lambda *a, **k: sockets.pop(0),
        pin_verifier=lambda *_args: None,
    )
    rpc._context = lambda: object()

    with pytest.raises(ProtocolError, match="truenas_refused"):
        rpc.status()
    assert rpc.status()["system_state"] == "online"
    assert sockets == []


def test_truenas_spki_mismatch_fails_before_api_key_is_sent():
    socket = RpcSocket()

    def reject(*_args):
        raise ProtocolError("server_identity_mismatch")

    rpc = TrueNasRpc(
        _config(),
        "read-key",
        connector=lambda *a, **k: socket,
        pin_verifier=reject,
    )
    with pytest.raises(ProtocolError, match="server_identity_mismatch"):
        rpc.status()
    assert socket.sent == []


def test_shutdown_gate_and_missing_separate_key_fail_before_network():
    socket = RpcSocket()
    with pytest.raises(ProtocolError, match="operation_disabled"):
        _rpc(_config(), socket).shutdown()
    with pytest.raises(ProtocolError, match="shutdown_credential_unavailable"):
        _rpc(
            _config(
                shutdown_enabled=True,
                truenas_shutdown_username="power_agent",
            ),
            socket,
        ).shutdown()
    assert socket.sent == []


def test_shutdown_uses_exact_reviewed_method_and_arguments():
    socket = RpcSocket(shutdown_result=None)
    result = _rpc(
        _config(
            shutdown_enabled=True,
            truenas_shutdown_username="power_agent",
        ),
        socket,
        "full-admin-key",
    ).shutdown()
    assert socket.sent[0]["params"][0]["username"] == "power_agent"
    call = socket.sent[-1]
    assert call["method"] == "system.shutdown"
    assert call["params"] == ["Butters NAS Agent approved shutdown", {"delay": None}]
    assert result == {
        "accepted": True,
        "state": "scheduled",
        "method": "system.shutdown",
    }


@pytest.mark.parametrize("acknowledgement", [True, 1, {}, {"job_id": 7}])
def test_shutdown_accepts_and_discards_non_null_success_acknowledgement(
    acknowledgement,
):
    socket = RpcSocket(shutdown_result=acknowledgement)
    result = _rpc(
        _config(
            shutdown_enabled=True,
            truenas_shutdown_username="power_agent",
        ),
        socket,
        "full-admin-key",
    ).shutdown()

    assert result == {
        "accepted": True,
        "state": "scheduled",
        "method": "system.shutdown",
    }
    assert socket.sent[-1]["method"] == "system.shutdown"
    assert socket.sent[-1]["params"] == [
        "Butters NAS Agent approved shutdown",
        {"delay": None},
    ]


def test_backend_refusal_is_enumerated_without_server_blob():
    socket = RpcSocket(error_method="system.state")
    with pytest.raises(ProtocolError, match="truenas_refused"):
        _rpc(_config(), socket).status()


def test_shutdown_backend_timeout_is_enumerated():
    def unavailable(*_args, **_kwargs):
        raise TimeoutError

    rpc = TrueNasRpc(
        _config(
            shutdown_enabled=True,
            truenas_shutdown_username="power_agent",
        ),
        "read-key",
        "full-admin-key",
        connector=unavailable,
    )
    rpc._context = object  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="timeout"):
        rpc.shutdown()


class Response:
    status = 200
    headers: ClassVar = {"X-Application-Version": "10.10.7"}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, maximum):
        assert maximum == 4096
        return b"secret-looking body that must not be returned"


def test_jellyfin_uses_fixed_url_and_never_returns_body():
    seen = {}

    def open_request(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    result = JellyfinBackend(_config(), opener=open_request).status()
    assert seen == {"url": "http://192.168.1.240:8096/health", "timeout": 5.0}
    assert result == {
        "reachable": True,
        "ready": True,
        "version": "10.10.7",
        "http_status": 200,
    }
    assert "body" not in result


def test_jellyfin_unavailable_is_not_system_unavailable():
    def unavailable(*args, **kwargs):
        raise OSError("offline")

    assert JellyfinBackend(_config(), opener=unavailable).status() == {
        "reachable": False,
        "ready": False,
        "version": None,
        "http_status": None,
    }


def test_truenas_interface_rate_uses_one_operator_owned_reporting_graph():
    rpc = TrueNasRpc(_config(network_monitoring_enabled=True), "read-key")
    seen = {}

    def read_call(method, params, *, deadline):
        seen.update(method=method, params=params, deadline=deadline)
        return [
            {
                "name": "interface",
                "identifier": "eno1",
                "data": [
                    [100, 1_000, 2_000],
                    [102, 2_000, 3_000],
                ],
                "aggregations": None,
                "start": 100,
                "end": 102,
                "legend": ["time", "received", "sent"],
            }
        ]

    rpc._read_call = read_call
    result = rpc.interface_rate()
    assert seen["method"] == "reporting.netdata_get_data"
    assert seen["params"][0] == [{"name": "interface", "identifier": "eno1"}]
    assert result["tx_mbps"] == 3.0
    assert result["rx_mbps"] == 2.0
    assert result["sample_window_seconds"] == 2
