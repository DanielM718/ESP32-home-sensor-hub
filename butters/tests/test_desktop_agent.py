from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest

from butters_agent.engine import Engine
from butters_agent.platform.fake import Platform
from butters_agent.protocol import (ProtocolError, ReplayCache, decode, envelope,
                                    parameters, request, sign, verify)
from butters.actions.agent import AgentHub


def frame(action="desktop.app.launch", values=None):
    return sign(envelope("request", "connection", request_id=str(uuid.uuid4()),
        action=action, target="desktop", parameters={"app": "git_bash"} if values is None else values,
        idempotency_key=str(uuid.uuid4()), timeout_seconds=30), b"k" * 32)


@pytest.fixture
def engine():
    engine = Engine(Platform())
    engine.apps = {"git_bash": {"path": "git-bash.exe", "images": ["mintty.exe"]},
                   "parsec": {"path": "parsecd.exe", "images": ["parsecd.exe"]}}
    return engine


@pytest.mark.parametrize("action,values", [
    ("shell", {}), ("desktop.app.launch", {"app": "C:\\bad.exe"}),
    ("desktop.app.launch", {"app": "parsec", "args": []}),
    ("desktop.app.launch", {"app": "x;shutdown"}),
    ("desktop.app.launch", {"app": None}), ("desktop.vm.stop", {"vm": "a", "force": True}),
    ("desktop.app.list", {"path": "x"}), ("desktop.app.launch", {}),
])
def test_no_arbitrary_execution(action, values):
    with pytest.raises(ProtocolError):
        parameters(action, values)


def test_signatures_bind_all_fields():
    original = frame()
    verify(original, b"k" * 32, "connection")
    request(original, target="desktop")
    for field, value in [("timeout_seconds", 29), ("idempotency_key", str(uuid.uuid4())),
                         ("parameters", {"app": "parsec"}), ("target", "other")]:
        with pytest.raises(ProtocolError):
            verify({**original, field: value}, b"k" * 32, "connection")


@pytest.mark.parametrize("delta", [-300, 300])
def test_clock_replay_window(delta):
    original = frame()
    with pytest.raises(ProtocolError, match="stale_request"):
        verify(original, b"k" * 32, "connection", now=time.time() + delta)


def test_connection_nonce_and_stale_action():
    original = frame()
    with pytest.raises(ProtocolError, match="superseded_connection"):
        verify(original, b"k" * 32, "new")
    with pytest.raises(ProtocolError, match="stale_request"):
        request(original, target="desktop", now=time.time() + 31)
    with pytest.raises(ProtocolError, match="wrong_target"):
        request(original, target="other")


@pytest.mark.parametrize("raw", ['[]', '{"x":1,"x":2}', '{"x":NaN}', 'bad', 'x' * 32769])
def test_malformed(raw):
    with pytest.raises(ProtocolError):
        decode(raw)


def test_idempotency_results_and_conflicts(engine):
    cache = ReplayCache()
    original = frame()
    result = engine.invoke(original["action"], original["parameters"])
    cache.put(original, result)
    assert cache.get(original) == result
    retry = {**original, "request_id": str(uuid.uuid4())}
    assert cache.get(retry) == result
    with pytest.raises(ProtocolError, match="duplicate_request"):
        cache.get({**original, "parameters": {"app": "parsec"}})
    assert engine.platform.launches == 1


def test_cache_bound_and_expiry():
    clock = [0]
    cache = ReplayCache(clock=lambda: clock[0], capacity=2)
    original = frame()
    cache.put(original, {})
    for _ in range(3):
        cache.put(frame(), {})
    assert len(cache.entries) == 4
    clock[0] = 301
    assert cache.get(original) is None
    assert not cache.entries


def test_already_running_observed(engine):
    assert engine.invoke("desktop.app.launch", {"app": "git_bash"})["success"]
    assert engine.invoke("desktop.app.launch", {"app": "git_bash"})["state"] == "already_running"
    assert engine.platform.launches == 1


def test_session_unavailable_missing_launch_failure(engine):
    engine.platform.active = False
    assert engine.invoke("desktop.app.launch", {"app": "parsec"})["error"] == "session_inactive"
    engine.platform.active = True
    engine.platform.missing.add("parsecd.exe")
    assert engine.invoke("desktop.app.launch", {"app": "parsec"})["error"] == "app_not_installed"
    engine.platform.missing.clear()
    engine.platform.failure = True
    assert engine.invoke("desktop.app.launch", {"app": "parsec"})["error"] == "launch_failed"
    assert engine.invoke("desktop.app.status", {"app": "unknown"})["error"] == "unknown_app"


def test_cancel_before_side_effect(engine):
    import threading
    cancel = threading.Event()
    cancel.set()
    assert engine.invoke("desktop.app.launch", {"app": "parsec"}, cancel)["error"] == "cancelled"
    assert engine.platform.launches == 0


def test_vm_unavailable_is_not_fabricated(engine):
    assert engine.invoke("desktop.vm.list", {})["vms"] == []
    assert engine.invoke("desktop.vm.start", {"vm": "unknown"})["error"] == "vm_unavailable"


def test_status_does_not_infer_gui_from_ssh(tmp_path):
    hub = AgentHub(tmp_path / "missing")
    assert not hub.status()["available"]
    assert not hub.status()["capabilities"]["gui_launch"]


def test_real_hub_client_protocol_duplicate_and_disconnect(tmp_path, engine):
    from butters_agent.client import Client
    import hashlib
    token = "a" * 64
    key_path = tmp_path / "key"
    key_path.write_text("6b" * 32)
    key_path.chmod(0o600)
    config = tmp_path / "agent.toml"
    config.write_text('schema_version=1\nagent_id="desktop"\ntoken_sha256="' +
        hashlib.sha256(token.encode()).hexdigest() + '"\ncommand_key_file="' + str(key_path) + '"\n')
    config.chmod(0o600)
    hub = AgentHub(config)
    assert hub.config, hub.reason

    async def scenario():
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        class ServerSocket:
            headers = {}
            async def accept(self): pass
            async def receive_text(self):
                raw = await incoming.get()
                if raw is None: raise ConnectionError()
                return raw
            async def send_text(self, raw): await outgoing.put(raw)
            async def close(self, code=1000): await outgoing.put(None)
        class ClientSocket:
            def __aiter__(self): return self
            async def __anext__(self):
                raw = await outgoing.get()
                if raw is None: raise StopAsyncIteration()
                return raw
            async def send(self, raw): await incoming.put(raw)
        await incoming.put(json.dumps({"type":"hello", "protocol":1, "schema":1,
            "agent_id":"desktop", "version":"0.1.0", "token":token,
            "actions":list(__import__("butters_agent.protocol", fromlist=["SCHEMAS"]).SCHEMAS)}))
        server = asyncio.create_task(hub.socket(ServerSocket()))
        raw_welcome = await outgoing.get()
        assert raw_welcome is not None, hub.reason
        welcome = decode(raw_welcome)
        client = Client({"agent_id":"desktop"}, {"command_key":"6b"*32}, engine)
        connection = asyncio.create_task(client._connected(ClientSocket(), welcome["connection_id"]))
        for _ in range(100):
            if hub.status()["available"]: break
            await asyncio.sleep(.01)
        rid, key = str(uuid.uuid4()), str(uuid.uuid4())
        result = await asyncio.to_thread(hub.invoke, "desktop.app.launch", {"app":"git_bash"},
                                          request_id=rid, idempotency_key=key)
        assert result["success"] is True
        duplicate = await asyncio.to_thread(hub.invoke, "desktop.app.launch", {"app":"git_bash"},
                                          request_id=rid, idempotency_key=key)
        assert duplicate["success"] is True
        duplicate = await asyncio.to_thread(hub.invoke, "desktop.app.launch", {"app":"git_bash"},
                                          idempotency_key=key)
        assert duplicate["success"] is True
        assert engine.platform.launches == 1
        await incoming.put(None)
        await server
        await connection
        assert not hub.status()["available"]
    asyncio.run(scenario())
    assert hub.invoke("desktop.app.launch", {"app": "parsec"})["error"] == "agent_unavailable"
    hub.ws = object()
    hub.last_seen = time.monotonic()
    hub.session = {"gui_launch": True, "interactive_session": True}
    assert hub.status()["capabilities"]["gui_launch"]
    hub.last_seen -= 46
    assert not hub.status()["available"]
    assert not hub.status()["capabilities"]["gui_launch"]


def test_tls_pin_rejection_sends_no_credentials(monkeypatch, engine):
    from butters_agent.client import Client
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1).not_valid_before(now)
        .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    sent = []
    class TLS:
        def getpeercert(self, binary_form):
            return cert.public_bytes(serialization.Encoding.DER)
    class Transport:
        def get_extra_info(self, name): return TLS()
    class Socket:
        transport = Transport()
        async def send(self, value): sent.append(value)
    class Connection:
        async def __aenter__(self): return Socket()
        async def __aexit__(self, *args): pass
    monkeypatch.setattr("butters_agent.client.connect", lambda *a, **k: Connection())
    client = Client({"url":"wss://butters.lan:8443/agent/v1/session",
        "spki_sha256":"0"*64}, {"command_key":"6b"*32, "token":"a"*64}, engine)
    with pytest.raises(ProtocolError, match="server_identity_mismatch"):
        asyncio.run(client.session())
    assert sent == []


def test_wrong_token_and_browser_origin_rejected(tmp_path):
    import hashlib
    key = tmp_path / "key"
    key.write_text("6b"*32)
    key.chmod(0o600)
    config = tmp_path / "agents.toml"
    config.write_text('schema_version=1\nagent_id="desktop"\ntoken_sha256="' +
        hashlib.sha256(b"a"*64).hexdigest() + '"\ncommand_key_file="' + str(key) + '"\n')
    config.chmod(0o600)
    from butters_agent.protocol import SCHEMAS
    class Socket:
        headers = {}
        sent = []
        closed = False
        async def accept(self): pass
        async def receive_text(self):
            return json.dumps({"type":"hello", "protocol":1, "schema":1,
                "agent_id":"desktop", "version":"0.1.0", "token":"b"*64, "actions":list(SCHEMAS)})
        async def send_text(self, frame): self.sent.append(frame)
        async def close(self, code): self.closed = True
    for headers in ({}, {"origin":"https://evil.invalid"}):
        hub, socket = AgentHub(config), Socket()
        socket.headers = headers
        asyncio.run(hub.socket(socket))
        assert socket.closed
        assert not socket.sent
        assert not hub.status()["available"]


@pytest.mark.parametrize("change", [
    ("GET /agent/v1/session", "GET /admin"), ("GET /agent/v1/session", "GET /api/desktop/actions"),
    ("GET /agent/v1/session", "GET /agent/v1/session?extra=1"),
    ("Host: butters", "Host: butters\r\nTailscale-User-Login: forged"),
    ("Host: butters", "Host: butters\r\nCookie: forged"),
    ("Host: butters", "Host: butters\r\nOrigin: https://evil.invalid"),
    ("Host: butters", "Host: butters\r\nAuthorization: forbidden"),
    ("Host: butters", "Host: butters\r\nContent-Length: 100"),
    ("Host: butters", "Host: butters\r\nHost: duplicate"),
])
def test_ingress_cannot_expose_browser_api(change):
    from butters.actions.agent_ingress import validate_handshake
    raw = ("GET /agent/v1/session HTTP/1.1\r\nHost: butters\r\nUpgrade: websocket\r\n"
           "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
           "Sec-WebSocket-Key: aaaaaaaaaaaaaaaaaaaaaa==\r\n\r\n")
    assert validate_handshake(raw.encode()).startswith(b"GET /agent/v1/session ")
    with pytest.raises(ValueError):
        validate_handshake(raw.replace(*change).encode())


@pytest.mark.parametrize("address", ["0.0.0.0", "8.8.8.8"])
def test_ingress_refuses_public_or_wildcard_binding(monkeypatch, address):
    from butters.actions.agent_ingress import bind_addresses
    monkeypatch.setattr("socket.gethostbyname_ex", lambda _: ("test", [], [address]))
    with pytest.raises(ValueError, match="private_lan_binding_required"):
        bind_addresses({"bind_host":"test"})


def test_agent_skill_registry_requires_existing_authentication(tmp_path, engine):
    from butters.skills.registry import SkillRegistry
    from butters.skills.policy import PolicyValidator
    from butters.skills.model import ActionClass, AuthenticationLevel, ActionAuthorization, AuthenticationContext
    from butters.skills.desktop_agent import register_agent_skills
    class Hub:
        def status(self): return {"available":True}
        def invoke(self, action, values, **kwargs): return engine.invoke(action, values)
    registry = SkillRegistry(PolicyValidator(allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})))
    register_agent_skills(registry, Hub())
    action = "desktop.app.launch"
    denied = registry.execute(action, {"app":"git_bash"}, administrator=True)
    assert not denied.ok
    assert engine.platform.launches == 0
    authorization = ActionAuthorization(frozenset({action}), "direct_user_request", True)
    denied = registry.execute(action, {"app":"git_bash"}, administrator=True, action_authorization=authorization)
    assert not denied.ok
    assert engine.platform.launches == 0
    context = AuthenticationContext(AuthenticationLevel.ELEVATED,"test","test",time.time()+30,"unit_test")
    result = registry.execute(action, {"app":"git_bash"}, administrator=True,
        action_authorization=authorization, authentication_context=context,session_id="test",identity="test")
    assert result.ok
    assert engine.platform.launches == 1
    assert registry.get("desktop.vm.stop").authentication is AuthenticationLevel.FRESH


def test_streaming_reuses_broker_and_agent_not_wol_implementation():
    from butters.actions.streaming import StreamingWorkflow
    from butters.actions.broker import BrokerOperation
    from butters.assistant_config import BrokerSettings
    from types import SimpleNamespace
    operations=[]
    class Compute:
        def execute(self, action): return {"online":True,"success":True}
    class Hub:
        def status(self): return {"agent_connected":True,"capabilities":{"gui_launch":True}}
        def invoke(self, action, params, **kwargs):
            operations.append(action)
            return {"success":True,"running":True}
    workflow=StreamingWorkflow(Compute(),Hub(),BrokerSettings())
    workflow.broker=SimpleNamespace(request=lambda operation, **kwargs:
        (operations.append(operation) or SimpleNamespace(ok=True,status={"plausibly_ready":True})))
    result=workflow.prepare()
    assert result["streaming_ready"]
    assert BrokerOperation.DESKTOP_PARSEC_ENSURE in operations
    assert BrokerOperation.DESKTOP_WAKE not in operations
    assert "desktop.app.launch" in operations
    assert result["steps"] == list(workflow.STEPS)
