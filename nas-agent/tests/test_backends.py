import json
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
        "truenas_ca_file": Path("/ca.pem"),
        "jellyfin_url": "http://192.168.1.240:8096",
        "jellyfin_health_path": "/health",
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

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def close(self):
        pass

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def recv(self, timeout):
        call = self.sent[-1]
        method = call["method"]
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
    rpc = TrueNasRpc(config, "read-key", shutdown_key, connector=lambda *a, **k: socket)
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


def test_shutdown_gate_and_missing_separate_key_fail_before_network():
    socket = RpcSocket()
    with pytest.raises(ProtocolError, match="operation_disabled"):
        _rpc(_config(), socket).shutdown()
    with pytest.raises(ProtocolError, match="shutdown_credential_unavailable"):
        _rpc(_config(shutdown_enabled=True), socket).shutdown()
    assert socket.sent == []


def test_shutdown_uses_exact_reviewed_method_and_arguments():
    socket = RpcSocket(shutdown_result=None)
    result = _rpc(_config(shutdown_enabled=True), socket, "full-admin-key").shutdown()
    call = socket.sent[-1]
    assert call["method"] == "system.shutdown"
    assert call["params"] == ["Butters NAS Agent approved shutdown", {"delay": None}]
    assert result == {
        "accepted": True,
        "state": "scheduled",
        "method": "system.shutdown",
    }


def test_backend_refusal_is_enumerated_without_server_blob():
    socket = RpcSocket(error_method="system.state")
    with pytest.raises(ProtocolError, match="truenas_refused"):
        _rpc(_config(), socket).status()


def test_shutdown_backend_timeout_is_enumerated():
    def unavailable(*_args, **_kwargs):
        raise TimeoutError

    rpc = TrueNasRpc(
        _config(shutdown_enabled=True),
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

    result = JellyfinBackend(_config(), open_request).status()
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

    assert JellyfinBackend(_config(), unavailable).status() == {
        "reachable": False,
        "ready": False,
        "version": None,
        "http_status": None,
    }
