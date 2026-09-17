import asyncio
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from butters_nas_agent.__main__ import SafeJsonFormatter
from butters_nas_agent.client import Client, healthcheck
from butters_nas_agent.config import load_agent_credentials, load_api_key, load_config
from butters_nas_agent.protocol import ProtocolError, decode, envelope, sign
from butters_nas_agent.tls import verify_spki


def _write(path: Path, value: str, mode=0o600):
    path.write_text(value)
    path.chmod(mode)
    return path


def _toml(**changes):
    values = {
        "agent_id": "nas-primary",
        "shutdown": "false",
        "shutdown_username": "",
        "path": "/nas-agent/v1/session",
    }
    values.update(changes)
    shutdown_username = (
        f'shutdown_username = "{values["shutdown_username"]}"\n'
        if values["shutdown_username"]
        else ""
    )
    return f'''schema_version = 1
[agent]
url = "wss://butters:8443{values["path"]}"
agent_id = "{values["agent_id"]}"
spki_sha256 = "{"a" * 64}"
heartbeat_seconds = 15
health_file = "/run/agent-health"
[truenas]
url = "wss://truenas.local/api/current"
username = "status_agent"
{shutdown_username}spki_sha256 = "{"b" * 64}"
timeout_seconds = 5
shutdown_enabled = {values["shutdown"]}
[jellyfin]
url = "http://192.168.1.240:8096"
health_path = "/health"
'''


def test_config_requires_exact_identity_paths_and_default_disabled(tmp_path):
    path = _write(tmp_path / "agent.toml", _toml())
    config = load_config(path)
    assert config.agent_id == "nas-primary"
    assert config.shutdown_enabled is False
    for changed in (
        _toml(agent_id="desktop"),
        _toml(path="/agent/v1/session"),
        _toml().replace("/api/current", "/api/v2.0/system/info"),
    ):
        _write(path, changed)
        with pytest.raises(ValueError, match="invalid_configuration"):
            load_config(path)


def test_enabled_shutdown_requires_a_distinct_fixed_identity(tmp_path):
    path = _write(
        tmp_path / "agent.toml",
        _toml(shutdown="true", shutdown_username="power_agent"),
    )
    config = load_config(path)
    assert config.shutdown_enabled is True
    assert config.truenas_username == "status_agent"
    assert config.truenas_shutdown_username == "power_agent"

    for changed in (
        _toml(shutdown="true"),
        _toml(shutdown="true", shutdown_username="status_agent"),
        _toml(shutdown_username="power_agent"),
    ):
        _write(path, changed)
        with pytest.raises(ValueError, match="invalid_configuration"):
            load_config(path)


def test_config_rejects_group_or_world_writable_file(tmp_path):
    path = _write(tmp_path / "agent.toml", _toml(), 0o622)
    with pytest.raises(ValueError, match="unsafe_configuration"):
        load_config(path)


def test_credentials_are_exact_and_private(tmp_path):
    path = _write(
        tmp_path / "credentials.json",
        json.dumps({"token": "a" * 64, "command_key": "b" * 64}),
    )
    assert load_agent_credentials(path)["token"] == "a" * 64
    _write(path, path.read_text(), 0o640)
    with pytest.raises(ValueError, match="unsafe_agent_credentials"):
        load_agent_credentials(path)


def test_api_key_is_private_and_not_whitespace_bearing(tmp_path):
    path = _write(tmp_path / "key", "1-secret-material")
    assert load_api_key(path) == "1-secret-material"
    _write(path, "secret with spaces")
    with pytest.raises(ValueError, match="invalid_truenas_api_key"):
        load_api_key(path)


def test_healthcheck_requires_recent_agent_written_marker(tmp_path):
    marker = _write(tmp_path / "heartbeat", "")
    stamp = marker.stat().st_mtime
    assert healthcheck(marker, now=stamp + 89)
    assert not healthcheck(marker, now=stamp + 91)
    assert not healthcheck(tmp_path / "missing", now=stamp)


def test_spki_pin_mismatch_fails_before_machine_auth(monkeypatch):
    public = b"reviewed-test-spki"

    class PublicKey:
        def public_bytes(self, *_args):
            return public

    class Certificate:
        def public_key(self):
            return PublicKey()

    class TlsObject:
        def getpeercert(self, *, binary_form):
            assert binary_form is True
            return b"certificate"

    monkeypatch.setattr(
        "butters_nas_agent.tls.x509.load_der_x509_certificate",
        lambda _value: Certificate(),
    )
    expected = hashlib.sha256(public).hexdigest()
    verify_spki(TlsObject(), expected)
    with pytest.raises(ProtocolError, match="server_identity_mismatch"):
        verify_spki(TlsObject(), "0" * 64)


def test_safe_formatter_preserves_timing_fields_but_drops_unapproved_values():
    record = logging.LogRecord(
        "butters_nas_agent",
        logging.INFO,
        __file__,
        1,
        json.dumps(
            {
                "event": "truenas_connect",
                "outcome": "connected",
                "address_kind": "ip_literal",
                "elapsed_ms": 12.345,
                "backend_ms": 15.0,
                "method": "system.info",
                "api_key": "must-not-appear",
                "url": "must-not-appear",
            }
        ),
        (),
        None,
    )
    value = json.loads(SafeJsonFormatter().format(record))
    assert value["event"] == "truenas_connect"
    assert value["outcome"] == "connected"
    assert value["address_kind"] == "ip_literal"
    assert value["elapsed_ms"] == 12.345
    assert value["backend_ms"] == 15.0
    assert value["method"] == "system.info"
    assert "api_key" not in value
    assert "url" not in value
    assert "must-not-appear" not in json.dumps(value)


def test_immediate_exact_retry_replays_cached_result_while_active(
    monkeypatch, tmp_path
):
    """A retry in the result-send cleanup window must not time out."""

    key = b"z" * 32
    connection_id = "c" * 64
    request_id = str(uuid.uuid4())
    idempotency_key = str(uuid.uuid4())
    closed = object()

    class BackendEngine:
        def __init__(self):
            self.invocations = 0

        def heartbeat_state(self):
            return {
                "system": {"reachable": False, "error": "truenas_unavailable"},
                "jellyfin": {"reachable": False, "error": "backend_unavailable"},
            }

        def heartbeat_sent(self, _sequence):
            return None

        def invoke(self, action, parameters, _cancel):
            self.invocations += 1
            assert action == "nas.system.shutdown"
            assert parameters == {}
            now = time.time()
            return {
                "action": action,
                "target": "nas-primary",
                "started_at": now,
                "success": True,
                "accepted": True,
                "state": "scheduled",
                "method": "system.shutdown",
                "completed_at": now,
                "duration_seconds": 0.0,
            }

    class Socket:
        def __init__(self):
            self.incoming = asyncio.Queue()
            self.sent = []
            self.first_result_started = asyncio.Event()
            self.release_first_result = asyncio.Event()

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.incoming.get()
            if value is closed:
                raise StopAsyncIteration
            return value

        async def send(self, raw):
            frame = decode(raw)
            self.sent.append(frame)
            if frame.get("type") == "result" and frame.get("duplicate") is False:
                self.first_result_started.set()
                await self.release_first_result.wait()

    async def inline_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    engine = BackendEngine()
    client = Client(
        SimpleNamespace(
            agent_id="nas-primary",
            heartbeat_seconds=3600,
            health_file=tmp_path / "health",
        ),
        {"command_key": key.hex()},
        engine,
    )
    socket = Socket()

    def request_frame():
        return sign(
            envelope(
                "request",
                connection_id,
                request_id=request_id,
                action="nas.system.shutdown",
                target="nas-primary",
                parameters={},
                idempotency_key=idempotency_key,
                timeout_seconds=30,
            ),
            key,
        )

    async def scenario():
        task = asyncio.create_task(client._connected(socket, connection_id))
        await socket.incoming.put(json.dumps(request_frame()))
        await asyncio.wait_for(socket.first_result_started.wait(), 1)
        await socket.incoming.put(json.dumps(request_frame()))
        deadline = asyncio.get_running_loop().time() + 1
        while not any(
            frame.get("type") == "result" and frame.get("duplicate") is True
            for frame in socket.sent
        ):
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0)
        socket.release_first_result.set()
        await socket.incoming.put(closed)
        await asyncio.wait_for(task, 1)

    asyncio.run(scenario())
    results = [frame for frame in socket.sent if frame.get("type") == "result"]
    assert engine.invocations == 1
    assert len(results) == 2
    assert {frame["duplicate"] for frame in results} == {False, True}
