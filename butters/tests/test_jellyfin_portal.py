"""Jellyfin access portal: authentication, RBAC, wake, and redirect safety.

The properties under test are the ones that keep a partner's access narrow: the
role authorizes NAS status and wake and nothing else, it never implies
administrator, wake is unreachable by GET, and the redirect is always one of two
configured URLs chosen from a trusted ingress rather than from the request.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from butters.assistant_config import (
    NasAgentIngressSettings,
    NasEndpointSettings,
    PortalSettings,
    load_assistant_settings,
)
from butters.auth.manager import AuthenticationVerification
from butters.auth.store import JELLYFIN_ACCESS, NAS_POWER
from butters.integrations.nas_status import (
    JellyfinState,
    NasAggregate,
    NasObservation,
    Reach,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.locality import Locality, LocalityClassifier, jellyfin_destination
from butters.web.portal import _portal_bandwidth
from butters.web.service import BetaAssistantService

PARTNER = "partner@example.com"
ADMIN = "admin@example.com"
LAN_URL = "http://192.168.1.50:8096"
TS_URL = "https://nas.tail0000.ts.net"

ENDPOINTS = NasEndpointSettings(
    lan_host="192.168.1.50",
    tailscale_host="nas.tail0000.ts.net",
    jellyfin_lan_url=LAN_URL,
    jellyfin_tailscale_url=TS_URL,
    cache_seconds=0.0,
).validated()


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


class FakeWebAuthn:
    def registration_options(self, **_kwargs):
        return {
            "challenge": "dGVzdA",
            "rp": {"id": "sensor-pi.tail9644cc.ts.net"},
            "user": {"id": "dXNlcg", "name": "x", "displayName": "x"},
        }

    def verify_registration(self, credential, **_kwargs):
        from butters.auth.manager import RegistrationVerification

        return RegistrationVerification(
            credential["id"].encode(), b"public", 0, "multi_device", True
        )

    def authentication_options(self, **_kwargs):
        return {
            "challenge": "dGVzdA",
            "rpId": "sensor-pi.tail9644cc.ts.net",
            "allowCredentials": [],
            "userVerification": "required",
        }

    def verify_authentication(self, credential, **_kwargs):
        if credential.get("uv") is not True:
            return AuthenticationVerification(0, None, None, False)
        return AuthenticationVerification(0, "multi_device", True, True)


class FakeNas:
    def __init__(self) -> None:
        self.wakes = 0
        self.observation = NasObservation(
            Reach.UNREACHABLE,
            Reach.UNREACHABLE,
            Reach.UNREACHABLE,
            JellyfinState.UNAVAILABLE,
            NasAggregate.OFFLINE,
            0.0,
            0.01,
        )

    def wake(self, _cancel=None):
        self.wakes += 1
        return {"accepted": True, "outcome": "wake_packet_sent"}

    def shutdown(self, _cancel=None):
        raise AssertionError("the portal must never reach NAS shutdown")

    def observe(self, *, wake_requested_at=None, refresh=False):
        return self.observation.safe_dict()

    def become_ready(self) -> None:
        self.observation = NasObservation(
            Reach.REACHABLE,
            Reach.REACHABLE,
            Reach.REACHABLE,
            JellyfinState.READY,
            NasAggregate.READY,
            0.0,
            0.01,
        )


class _MockShutdownBackend:
    """A fixed, local-only backend for end-to-end control-plane acceptance."""

    def __init__(self) -> None:
        self.shutdowns = 0

    def status(self) -> dict[str, object]:
        return {
            "reachable": True,
            "hostname": "mock-truenas",
            "version": "25.10.7",
            "uptime_seconds": 1234.0,
            "system_state": "online",
        }

    def shutdown(self) -> dict[str, object]:
        self.shutdowns += 1
        return {
            "accepted": True,
            "state": "scheduled",
            "method": "system.shutdown",
        }


class _MockLocalBackend:
    def status(self) -> dict[str, object]:
        return {"hostname": "mock-agent", "uptime_seconds": 42.0}


class _MockJellyfinBackend:
    def status(self) -> dict[str, object]:
        return {
            "reachable": True,
            "ready": True,
            "version": "10.10.7",
            "http_status": 200,
        }


_CLOSED = object()


class _HubSocket:
    """The ASGI half of a bounded in-memory WebSocket pair."""

    def __init__(self, from_agent: asyncio.Queue, to_agent: asyncio.Queue) -> None:
        self.headers: dict[str, str] = {}
        self._from_agent = from_agent
        self._to_agent = to_agent
        self._closed = False

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        value = await self._from_agent.get()
        if value is _CLOSED:
            raise ConnectionError("mock agent disconnected")
        assert isinstance(value, str)
        return value

    async def send_text(self, value: str) -> None:
        if self._closed:
            raise ConnectionError("mock hub socket closed")
        await self._to_agent.put(value)

    async def close(self, code: int = 1000) -> None:
        del code
        if not self._closed:
            self._closed = True
            await self._to_agent.put(_CLOSED)


class _AgentSocket:
    """The standalone-client half of a bounded in-memory WebSocket pair."""

    def __init__(self, to_hub: asyncio.Queue, from_hub: asyncio.Queue) -> None:
        self._to_hub = to_hub
        self._from_hub = from_hub
        self._closed = False

    async def send(self, value: str) -> None:
        if self._closed:
            raise ConnectionError("mock agent socket closed")
        await self._to_hub.put(value)

    async def recv(self) -> str:
        value = await self._from_hub.get()
        if value is _CLOSED:
            raise ConnectionError("mock hub disconnected")
        assert isinstance(value, str)
        return value

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        value = await self._from_hub.get()
        if value is _CLOSED:
            raise StopAsyncIteration
        assert isinstance(value, str)
        return value

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._to_hub.put(_CLOSED)


async def _mock_agent_connection(service, tmp_path: Path, system_backend):
    """Attach the real standalone client/engine to the real hub, without a network."""

    source = Path(__file__).resolve().parents[2] / "nas-agent" / "src"
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    from butters_nas_agent import AGENT_VERSION
    from butters_nas_agent.client import Client
    from butters_nas_agent.engine import Engine as NasEngine
    from butters_nas_agent.protocol import (
        ACTION_SCHEMA_VERSION,
        PROTOCOL_VERSION,
        SCHEMAS,
        canonical,
        decode,
        verify,
    )

    agent_to_hub: asyncio.Queue = asyncio.Queue(maxsize=32)
    hub_to_agent: asyncio.Queue = asyncio.Queue(maxsize=32)
    hub_socket = _HubSocket(agent_to_hub, hub_to_agent)
    agent_socket = _AgentSocket(agent_to_hub, hub_to_agent)
    engine = NasEngine(_MockLocalBackend(), system_backend, _MockJellyfinBackend())
    config = SimpleNamespace(
        agent_id="nas-primary",
        heartbeat_seconds=0.05,
        health_file=tmp_path / "mock-agent.health",
    )
    credentials = {"token": "n" * 64, "command_key": (b"z" * 32).hex()}
    client = Client(config, credentials, engine)

    async def agent_session() -> None:
        await agent_socket.send(
            canonical(
                {
                    "type": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "schema": ACTION_SCHEMA_VERSION,
                    "agent_id": "nas-primary",
                    "version": AGENT_VERSION,
                    "token": credentials["token"],
                    "actions": sorted(SCHEMAS),
                }
            ).decode("ascii")
        )
        welcome = decode(await agent_socket.recv())
        connection_id = welcome["connection_id"]
        assert isinstance(connection_id, str)
        verify(welcome, bytes.fromhex(credentials["command_key"]), connection_id)
        assert welcome["protocol"] == PROTOCOL_VERSION
        client.connection_count += 1
        engine.connected(client.connection_count)
        await client._connected(agent_socket, connection_id)

    hub_task = asyncio.create_task(service.nas_agent.socket(hub_socket))
    agent_task = asyncio.create_task(agent_session())
    assert await _settles(lambda: service.nas_agent.status()["state"] == "connected")
    return agent_socket, hub_task, agent_task


def _nas_agent_credentials(tmp_path: Path) -> Path:
    key = tmp_path / "nas-agent-command.key"
    key.write_text((b"z" * 32).hex(), encoding="utf-8")
    key.chmod(0o640)
    token = "n" * 64
    config = tmp_path / "nas-agent.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                "protocol_version = 1",
                'agent_id = "nas-primary"',
                f'token_sha256 = "{hashlib.sha256(token.encode()).hexdigest()}"',
                f'command_key_file = "{key}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    config.chmod(0o640)
    return config


def _application(
    tmp_path,
    *,
    portal_settings: PortalSettings | None = None,
    nas_agent_shutdown: bool = False,
):
    base = load_assistant_settings()
    device = replace(base.actions.nas, enabled=True, configured=True)
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        actions=replace(
            base.actions,
            nas=device,
            nas_shutdown=replace(
                device,
                enabled=nas_agent_shutdown,
                configured=nas_agent_shutdown,
            ),
        ).validated(),
        nas_agent_ingress=NasAgentIngressSettings(
            enabled=nas_agent_shutdown,
            config_path=(
                _nas_agent_credentials(tmp_path)
                if nas_agent_shutdown
                else base.nas_agent_ingress.config_path
            ),
            request_timeout_seconds=2 if nas_agent_shutdown else 30,
        ).validated(),
        nas_endpoints=ENDPOINTS,
        portal=(portal_settings or PortalSettings(enabled=True)).validated(),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=(ADMIN,),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings, vocabulary, general_reasoner=NoCloud(), state_dir=tmp_path
    )
    service.passkeys.backend = FakeWebAuthn()
    nas = FakeNas()
    service.assistant.skills.get("wake_nas").implementation.__self__.nas = nas
    service.assistant.nas_adapter = nas
    return (
        create_app(settings, vocabulary, service, stt_engine_factory=Engine),
        service,
        nas,
    )


def test_admin_refresh_exposes_only_projected_read_diagnostics(tmp_path) -> None:
    _app, service, _nas = _application(tmp_path)
    calls: list[str] = []

    def result(action: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(action)
        return {
            "action": action,
            "success": True,
            "request_id": "internal-request-id",
            "transport": "nas_agent_wss",
            **payload,
        }

    service.nas_agent = SimpleNamespace(
        configured=True,
        status=lambda: {"state": "connected"},
        network_status=lambda: result(
            "nas.network.status",
            {
                "remote_tx_mbps": 1.25,
                "measurement_quality": "partial",
            },
        ),
        jellyfin_sessions=lambda: result(
            "nas.jellyfin.sessions",
            {"available": False, "reason": "jellyfin_authentication_failed"},
        ),
        bandwidth_status=lambda: result(
            "nas.bandwidth.status",
            {"policy_mode": "dry_run", "measurement_quality": "unavailable"},
        ),
    )

    status = service.nas_admin_status(refresh=True)
    assert calls == [
        "nas.network.status",
        "nas.jellyfin.sessions",
        "nas.bandwidth.status",
    ]
    assert status["network_telemetry"] == {
        "success": True,
        "remote_tx_mbps": 1.25,
        "measurement_quality": "partial",
    }
    assert status["jellyfin_sessions"] == {
        "success": True,
        "available": False,
        "reason": "jellyfin_authentication_failed",
    }
    assert "request_id" not in status["network_telemetry"]
    assert "transport" not in status["jellyfin_sessions"]


def _enroll(
    service,
    identity=PARTNER,
    credential_id=b"partner-credential",
    roles=frozenset({JELLYFIN_ACCESS}),
):
    """Grant the role and register a credential, as enrollment would."""

    service.auth_state.grant_portal_roles(
        f"identity:{identity}", "Partner", roles, maximum=16
    )
    service.auth_state.add_credential(
        credential_id=credential_id,
        public_key=b"public",
        user_id=b"user",
        identity=f"identity:{identity}",
        label="Partner phone",
        sign_count=0,
        device_type="multi_device",
        backed_up=True,
    )


def _credential(credential_id=b"partner-credential") -> dict[str, object]:
    return {
        "id": base64.urlsafe_b64encode(credential_id).rstrip(b"=").decode(),
        "uv": True,
    }


async def _mutation(http, identity):
    headers = {"tailscale-user-login": identity}
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _settles(predicate, *, timeout: float = 5.0) -> bool:
    """Wait for a coordinator-scheduled effect.

    ActionCoordinator.execute() returns as soon as the job is queued and the
    skill runs on a worker thread, so a counter assertion immediately after the
    POST is a race under load rather than a real expectation.
    """

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _sign_in(http, mutation, credential_id=b"partner-credential"):
    begin = await http.post(
        "/api/portal/authenticate/options", headers=mutation, json={}
    )
    assert begin.status_code == 200, begin.text
    return await http.post(
        "/api/portal/authenticate/verify",
        headers=mutation,
        json={
            "ceremony_id": begin.json()["value"]["ceremony_id"],
            "credential": _credential(credential_id),
        },
    )


# --------------------------- authentication / RBAC ---------------------------


async def _unauthenticated_access_is_denied(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            for method, path in (
                ("GET", "/api/portal/nas"),
                ("GET", "/api/portal/destination"),
                ("POST", "/api/portal/wake"),
            ):
                response = await (
                    http.get(path, headers=mutation)
                    if method == "GET"
                    else http.post(path, headers=mutation, json={})
                )
                assert response.status_code == 401, path
                assert response.json()["error"] == "portal_authentication_required"
            assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


async def _valid_role_is_accepted_and_grants_nothing_more(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            verified = await _sign_in(http, mutation)
            assert verified.status_code == 200
            body = verified.json()["value"]
            assert body["roles"] == [JELLYFIN_ACCESS]
            # A portal sign-in confers no administrator rights whatsoever.
            assert body["administrator"] is False

            state = await http.get("/api/portal/nas", headers=mutation)
            assert state.status_code == 200
            assert state.json()["aggregate"] == "OFFLINE"

            # Administrator surfaces stay closed to this identity.
            for path in (
                "/admin",
                "/api/admin/overview",
                "/api/admin/tools/desktop",
                "/api/admin/tools/nas",
                "/api/admin/portal/identities",
            ):
                denied = await http.get(path, headers=mutation)
                assert denied.status_code in {401, 403}, path

            # The shared capability endpoint is pre-existing and answers any
            # tailnet session with the non-administrator view; holding the
            # portal role changes nothing about it. What matters is that the
            # role cannot INVOKE anything, so ask the assistant to run one.
            catalog = await http.get("/api/capabilities", headers=mutation)
            assert catalog.status_code == 200
            assert catalog.json()["capabilities"]
            refused = await http.post(
                "/api/chat",
                headers=mutation,
                json={"text": "shut down my desktop"},
            )
            assert refused.status_code == 200
            assert refused.json()["route"] == "action_denied"
            assert "administrator_required" in refused.json()["reason_codes"]

            for path in (
                "/api/admin/tools/shutdown-nas",
                "/api/admin/tools/wake-nas",
                "/api/admin/tools/desktop/wake",
                "/api/admin/tools/desktop/shutdown",
                "/api/admin/tools/desktop/launch-app",
                "/api/admin/portal/invite",
            ):
                denied = await http.post(path, headers=mutation, json={})
                assert denied.status_code in {400, 401, 403}, path

            # The portal itself has no shutdown route at all.
            missing = await http.post("/api/portal/shutdown", headers=mutation, json={})
            assert missing.status_code == 404
            assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


async def _revocation_takes_effect_immediately(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)
            assert (
                await http.get("/api/portal/nas", headers=mutation)
            ).status_code == 200

            # Revoking the role ends the live session on the next request,
            # without waiting for the portal session to expire.
            service.auth_state.revoke_portal_identity(f"identity:{PARTNER}")
            denied = await http.get("/api/portal/nas", headers=mutation)
            assert denied.status_code == 401
            refused = await http.post("/api/portal/wake", headers=mutation, json={})
            assert refused.status_code == 401
            assert nas.wakes == 0

            # And a fresh sign-in is refused too.
            begin = await http.post(
                "/api/portal/authenticate/options", headers=mutation, json={}
            )
            assert begin.status_code == 403
            assert begin.json()["error"] == "role_denied"
    finally:
        await app.state.shutdown_workers()
        del service


async def _registration_requires_an_invitation(tmp_path) -> None:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            for token in ("", "guessed-token"):
                refused = await http.post(
                    "/api/portal/register/options",
                    headers=mutation,
                    json={"label": "Phone", "invite_token": token},
                )
                assert refused.status_code in {400, 403}, token
            assert service.auth_state.portal_roles(f"identity:{PARTNER}") == frozenset()
    finally:
        await app.state.shutdown_workers()
        del service


async def _admin_invitation_enrolls_exactly_one_identity(tmp_path) -> None:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as admin_http:
            admin = await _mutation(admin_http, ADMIN)
            created = await admin_http.post(
                "/api/admin/portal/invite",
                headers=admin,
                json={"identity": f"identity:{PARTNER}", "label": "Partner"},
            )
            assert created.status_code == 200
            token = created.json()["invite_token"]
            assert created.json()["roles"] == [JELLYFIN_ACCESS]

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as other_http:
            # The invitation is bound to one identity; nobody else may use it.
            other = await _mutation(other_http, "stranger@example.com")
            stolen = await other_http.post(
                "/api/portal/register/options",
                headers=other,
                json={"label": "Phone", "invite_token": token},
            )
            assert stolen.status_code in {400, 403}

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            begin = await http.post(
                "/api/portal/register/options",
                headers=mutation,
                json={"label": "Partner phone", "invite_token": token},
            )
            assert begin.status_code == 200
            finish = await http.post(
                "/api/portal/register/verify",
                headers=mutation,
                json={
                    "ceremony_id": begin.json()["value"]["ceremony_id"],
                    "credential": {"id": "partner-credential"},
                },
            )
            assert finish.status_code == 200
            assert finish.json()["value"]["portal_roles"] == [JELLYFIN_ACCESS]
            assert service.auth_state.portal_roles(f"identity:{PARTNER}") == frozenset(
                {JELLYFIN_ACCESS}
            )

            # Single use: the same token cannot enroll a second credential.
            replayed = await http.post(
                "/api/portal/register/options",
                headers=mutation,
                json={"label": "Second", "invite_token": token},
            )
            assert replayed.status_code in {400, 403}
    finally:
        await app.state.shutdown_workers()
        del service


# --------------------------------- wake --------------------------------------


async def _wake_is_post_only_and_csrf_protected(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            # A prefetch, crawler, link preview or <img> is a GET; there is no
            # GET route for wake at all.
            for method in ("GET", "HEAD", "PUT", "DELETE"):
                response = await http.request(
                    method, "/api/portal/wake", headers=mutation
                )
                assert response.status_code in {404, 405}, method
                assert nas.wakes == 0

            # A cross-origin form POST fails the Origin check.
            cross = await http.post(
                "/api/portal/wake",
                headers={**mutation, "origin": "https://evil.example"},
                json={},
            )
            assert cross.status_code == 403
            assert cross.json()["error"] == "origin_denied"

            # A same-origin POST without the CSRF header fails too.
            no_csrf = {k: v for k, v in mutation.items() if k != "x-butters-csrf"}
            refused = await http.post("/api/portal/wake", headers=no_csrf, json={})
            assert refused.status_code == 403
            assert refused.json()["error"] == "csrf_denied"
            assert nas.wakes == 0

            accepted = await http.post("/api/portal/wake", headers=mutation, json={})
            assert accepted.status_code == 200
            body = accepted.json()
            assert body["status"] == "wake_packet_sent"
            assert body["message"] == "Wake packet sent"
            # It never claims the NAS came up.
            assert "booted" not in repr(body).lower()
            assert await _settles(lambda: nas.wakes == 1)
    finally:
        await app.state.shutdown_workers()
        del service


async def _wake_takes_no_parameters_and_is_rate_limited(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)
            for payload in (
                {"mac": "00:e2:69:7d:40:cd"},
                {"ip": "192.168.1.50"},
                {"host": "nas"},
                {"broadcast": "192.168.1.255"},
                {"skill": "shutdown_nas"},
                {"redirect": "https://evil.example"},
            ):
                refused = await http.post(
                    "/api/portal/wake", headers=mutation, json=payload
                )
                assert refused.status_code == 400, payload
            assert nas.wakes == 0

            # Bounded: repeated wakes are throttled rather than amplified.
            outcomes = []
            for _ in range(6):
                response = await http.post(
                    "/api/portal/wake", headers=mutation, json={}
                )
                outcomes.append(response.status_code)
            assert 429 in outcomes
            assert await _settles(lambda: 0 < nas.wakes < 6)
    finally:
        await app.state.shutdown_workers()
        del service


async def _polling_progresses_and_bounds_itself(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            offline = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert offline["headline"] == "NAS OFFLINE"
            assert offline["detail"] == "Jellyfin Unavailable"
            assert offline["jellyfin_ready"] is False
            assert offline["can_wake"] is True
            assert offline["can_shutdown"] is False

            await http.post("/api/portal/wake", headers=mutation, json={})
            waiting = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert waiting["last_operation"]["operation"] == "wake_nas"
            assert waiting["wake_elapsed_seconds"] is not None
            assert waiting["max_poll_seconds"] > 0
            # Wake Again is withheld while a wake is still in progress, so the
            # client cannot be nudged into re-sending packets.
            assert waiting["can_wake"] is False

            nas.become_ready()
            ready = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert ready["aggregate"] == "READY"
            assert ready["headline"] == "NAS Online"
            assert ready["detail"] == "Jellyfin Ready"
            assert ready["jellyfin_ready"] is True
            # Current state won, even though the wake record is still present.
            assert ready["last_operation"]["operation"] == "wake_nas"

            # The whole sequence sent exactly one packet.
            assert await _settles(lambda: nas.wakes == 1)

            # And timing out is reported, never retried automatically.
            service._last_operations["nas"]["at"] -= 10_000
            nas.observation = FakeNas().observation
            expired = (await http.get("/api/portal/nas", headers=mutation)).json()
            assert expired["poll_expired"] is True
            assert expired["can_wake"] is True
            assert nas.wakes == 1
    finally:
        await app.state.shutdown_workers()
        del service


# -------------------------------- redirect -----------------------------------


async def _redirect_only_when_ready_and_never_from_request_input(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    _enroll(service)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _mutation(http, PARTNER)
            await _sign_in(http, mutation)

            not_ready = (
                await http.get("/api/portal/destination", headers=mutation)
            ).json()
            assert not_ready["ready"] is False
            assert not_ready["destination"] is None

            nas.become_ready()
            # Every override a caller might try is simply not read.
            hostile = (
                await http.get(
                    "/api/portal/destination"
                    "?redirect=https://evil.example&host=evil.example"
                    "&ip=10.0.0.1&local=true&remote=true&url=https://evil.example",
                    headers={
                        **mutation,
                        "x-forwarded-for": "192.168.1.10",
                        "x-real-ip": "192.168.1.10",
                    },
                )
            ).json()
            assert hostile["ready"] is True
            assert hostile["destination"] in {LAN_URL, TS_URL}
            assert "evil.example" not in hostile["destination"]
            # With no trustworthy classification available, it fails safe.
            assert hostile["destination"] == TS_URL
            assert hostile["locality"] == "unknown"
    finally:
        await app.state.shutdown_workers()
        del service


async def _nas_power_is_independent_and_freezes_only_the_fixed_plan(tmp_path) -> None:
    app, service, _nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        # The intended initial partner role can use Jellyfin but cannot even
        # prepare a shutdown plan.
        _enroll(service)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as jellyfin_http:
            mutation = await _mutation(jellyfin_http, PARTNER)
            await _sign_in(jellyfin_http, mutation)
            denied = await jellyfin_http.post(
                "/api/portal/shutdown/plan",
                headers=mutation,
                json={"confirm": True},
            )
            assert denied.status_code == 401
            assert denied.json()["error"] == "portal_authentication_required"

        power_identity = "power@example.com"
        _enroll(
            service,
            identity=power_identity,
            credential_id=b"power-credential",
            roles=frozenset({NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as power_http:
            mutation = await _mutation(power_http, power_identity)
            verified = await _sign_in(
                power_http, mutation, credential_id=b"power-credential"
            )
            assert verified.json()["value"]["roles"] == [NAS_POWER]

            # NAS power does not imply Jellyfin access or administrator status.
            assert (
                await power_http.get("/api/portal/nas", headers=mutation)
            ).status_code == 401
            assert (
                await power_http.get("/api/admin/overview", headers=mutation)
            ).status_code in {
                401,
                403,
            }

            for payload in (
                {},
                {"confirm": False},
                {"confirm": True, "method": "system.reboot"},
                {"confirm": True, "delay": 0},
            ):
                rejected = await power_http.post(
                    "/api/portal/shutdown/plan", headers=mutation, json=payload
                )
                assert rejected.status_code in {400, 403}

            prepared = await power_http.post(
                "/api/portal/shutdown/plan",
                headers=mutation,
                json={"confirm": True},
            )
            assert prepared.status_code == 200, prepared.text
            pending = prepared.json()["pending_action"]
            assert prepared.json()["authentication_required"] == "fresh"
            assert pending["authentication"] == "fresh"
            assert pending["steps"] == [
                {"skill": "nas.system.shutdown", "arguments": {}}
            ]

            options = await power_http.post(
                "/api/portal/shutdown/authenticate/options",
                headers=mutation,
                json={"pending_action_id": pending["pending_action_id"]},
            )
            assert options.status_code == 200
            ceremony = options.json()["value"]["ceremony_id"]
            finished = await power_http.post(
                "/api/portal/shutdown/authenticate/verify",
                headers=mutation,
                json={
                    "ceremony_id": ceremony,
                    "credential": _credential(b"power-credential"),
                },
            )
            assert finished.status_code == 200, finished.text
            assert finished.json()["value"]["status"] == "shutdown_queued"

            # Mutation defenses apply to this new path too.
            no_csrf = await power_http.post(
                "/api/portal/shutdown/plan",
                headers={"tailscale-user-login": power_identity},
                json={"confirm": True},
            )
            assert no_csrf.status_code == 403
            wrong_origin = await power_http.post(
                "/api/portal/shutdown/plan",
                headers={**mutation, "origin": "https://attacker.invalid"},
                json={"confirm": True},
            )
            assert wrong_origin.status_code == 403

        # A separately assigned user holding both portal roles receives only
        # the fixed gated UI capability; neither role implies the other.
        combined_identity = "combined@example.com"
        _enroll(
            service,
            identity=combined_identity,
            credential_id=b"combined-credential",
            roles=frozenset({JELLYFIN_ACCESS, NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as combined_http:
            mutation = await _mutation(combined_http, combined_identity)
            await _sign_in(
                combined_http, mutation, credential_id=b"combined-credential"
            )
            state = await combined_http.get("/api/portal/nas", headers=mutation)
            assert state.status_code == 200
            assert state.json()["can_shutdown"] is True
    finally:
        await app.state.shutdown_workers()
        del service


async def _mock_shutdown_crosses_the_complete_signed_control_plane(tmp_path) -> None:
    """Accept shutdown only through the frozen plan and a mock local backend."""

    app, service, nas = _application(tmp_path, nas_agent_shutdown=True)
    backend = _MockShutdownBackend()
    first_connection = None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    power_identity = "power@example.com"
    identity_key = f"identity:{power_identity}"
    try:
        first_connection = await _mock_agent_connection(service, tmp_path, backend)
        nas.become_ready()
        _enroll(
            service,
            identity=power_identity,
            credential_id=b"power-credential",
            roles=frozenset({NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as power_http:
            mutation = await _mutation(power_http, power_identity)
            signed_in = await _sign_in(
                power_http, mutation, credential_id=b"power-credential"
            )
            assert signed_in.status_code == 200
            assert signed_in.json()["value"]["roles"] == [NAS_POWER]
            assert signed_in.json()["value"]["administrator"] is False

            prepared = await power_http.post(
                "/api/portal/shutdown/plan",
                headers=mutation,
                json={"confirm": True},
            )
            assert prepared.status_code == 200, prepared.text
            pending = prepared.json()["pending_action"]
            assert pending["authentication"] == "fresh"
            assert pending["steps"] == [
                {"skill": "nas.system.shutdown", "arguments": {}}
            ]

            options = await power_http.post(
                "/api/portal/shutdown/authenticate/options",
                headers=mutation,
                json={"pending_action_id": pending["pending_action_id"]},
            )
            assert options.status_code == 200
            finished = await power_http.post(
                "/api/portal/shutdown/authenticate/verify",
                headers=mutation,
                json={
                    "ceremony_id": options.json()["value"]["ceremony_id"],
                    "credential": _credential(b"power-credential"),
                },
            )
            assert finished.status_code == 200, finished.text
            value = finished.json()["value"]
            assert value["status"] == "shutdown_queued"
            assert len(value["jobs"]) == 1
            job_id = value["jobs"][0]["job_id"]
            action_state = service.action_state
            assert await _settles(
                lambda: (
                    next(
                        job
                        for job in action_state.jobs(identity=identity_key)
                        if job["job_id"] == job_id
                    )["state"]
                    == "completed"
                )
            )
            assert backend.shutdowns == 1

        accepted = service.nas_agent.status()
        assert not first_connection[2].done()
        assert accepted["shutdown_accepted_at"] is not None
        assert accepted["system"]["system_state"] == "shutting_down"
        assert service.nas_admin_status()["lifecycle"] == "SHUTTING_DOWN"
        # Extra parameters are rejected before any frame can reach the agent.
        rejected = service.nas_agent._request(
            "nas.system.shutdown", {"method": "system.reboot"}
        )
        assert rejected["error"] == "invalid_parameter"
        assert backend.shutdowns == 1
    finally:
        for connection in (first_connection,):
            if connection is None:
                continue
            await connection[0].close()
            for task in connection[1:]:
                task.cancel()
            await asyncio.gather(*connection[1:], return_exceptions=True)
        await app.state.shutdown_workers()
        del service


def test_unauthenticated_access_is_denied(tmp_path) -> None:
    asyncio.run(_unauthenticated_access_is_denied(tmp_path))


def test_valid_role_is_accepted_and_grants_nothing_more(tmp_path) -> None:
    asyncio.run(_valid_role_is_accepted_and_grants_nothing_more(tmp_path))


def test_revocation_takes_effect_immediately(tmp_path) -> None:
    asyncio.run(_revocation_takes_effect_immediately(tmp_path))


def test_registration_requires_an_invitation(tmp_path) -> None:
    asyncio.run(_registration_requires_an_invitation(tmp_path))


def test_admin_invitation_enrolls_exactly_one_identity(tmp_path) -> None:
    asyncio.run(_admin_invitation_enrolls_exactly_one_identity(tmp_path))


def test_wake_is_post_only_and_csrf_protected(tmp_path) -> None:
    asyncio.run(_wake_is_post_only_and_csrf_protected(tmp_path))


def test_wake_takes_no_parameters_and_is_rate_limited(tmp_path) -> None:
    asyncio.run(_wake_takes_no_parameters_and_is_rate_limited(tmp_path))


def test_polling_progresses_and_bounds_itself(tmp_path) -> None:
    asyncio.run(_polling_progresses_and_bounds_itself(tmp_path))


def test_redirect_only_when_ready_and_never_from_request_input(tmp_path) -> None:
    asyncio.run(_redirect_only_when_ready_and_never_from_request_input(tmp_path))


def test_nas_power_is_independent_and_freezes_only_the_fixed_plan(tmp_path) -> None:
    asyncio.run(_nas_power_is_independent_and_freezes_only_the_fixed_plan(tmp_path))


def test_mock_shutdown_crosses_the_complete_signed_control_plane(tmp_path) -> None:
    # CPython 3.13's Runner waits on the default executor's join helper even
    # after all to_thread calls have returned in this test environment. Close
    # the loop first, then join that same executor directly and deterministically.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            _mock_shutdown_crosses_the_complete_signed_control_plane(tmp_path)
        )
    finally:
        executor = loop._default_executor
        loop.close()
        if executor is not None:
            executor.shutdown(wait=True)


# ------------------------- locality classification ---------------------------


def _headers(values: dict[str, str]):
    class _Headers:
        def get(self, name, default=None):
            return values.get(name.lower(), default)

    return _Headers()


PORTAL = PortalSettings(
    enabled=True,
    lan_networks=("192.168.1.0/24",),
    locality_header="X-Butters-Locality",
    tailscale_status_command=("/usr/bin/tailscale", "status", "--json"),
).validated()


def test_untrusted_peer_is_never_classified_as_lan() -> None:
    classifier = LocalityClassifier(PORTAL, trusted_peers=frozenset({"127.0.0.1"}))
    decision = classifier.classify(
        _headers(
            {
                "x-butters-locality": "lan",
                "x-forwarded-for": "192.168.1.10",
                "x-real-ip": "192.168.1.10",
                "tailscale-user-login": PARTNER,
            }
        ),
        "203.0.113.9",
    )
    assert decision.locality is Locality.UNKNOWN
    assert decision.source == "untrusted_ingress"
    assert jellyfin_destination(decision, ENDPOINTS) == TS_URL


def test_trusted_ingress_may_state_locality() -> None:
    classifier = LocalityClassifier(PORTAL, trusted_peers=frozenset({"127.0.0.1"}))
    lan = classifier.classify(_headers({"x-butters-locality": "lan"}), "127.0.0.1")
    assert lan.locality is Locality.LAN
    assert jellyfin_destination(lan, ENDPOINTS) == LAN_URL
    tailnet = classifier.classify(
        _headers({"x-butters-locality": "tailnet"}), "127.0.0.1"
    )
    assert tailnet.locality is Locality.TAILNET
    assert jellyfin_destination(tailnet, ENDPOINTS) == TS_URL


def test_tailscaled_endpoint_decides_locality_without_client_input() -> None:
    def runner(argv, **_kwargs):
        payload = (
            '{"User":{"7":{"LoginName":"partner@example.com"}},'
            '"Peer":{"a":{"UserID":7,"Online":true,"CurAddr":"192.168.1.23:41641"}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload)

    classifier = LocalityClassifier(
        replace(PORTAL, locality_header=""),
        trusted_peers=frozenset({"127.0.0.1"}),
        runner=runner,
    )
    decision = classifier.classify(
        _headers({"tailscale-user-login": PARTNER}), "127.0.0.1"
    )
    assert decision.locality is Locality.LAN
    assert decision.source == "tailscaled_endpoint"
    assert jellyfin_destination(decision, ENDPOINTS) == LAN_URL


def test_remote_endpoint_is_tailnet_and_failures_fail_safe() -> None:
    def remote(argv, **_kwargs):
        payload = (
            '{"User":{"7":{"LoginName":"partner@example.com"}},'
            '"Peer":{"a":{"UserID":7,"Online":true,"CurAddr":"203.0.113.9:41641"}}}'
        )
        return subprocess.CompletedProcess(argv, 0, stdout=payload)

    def broken(argv, **_kwargs):
        raise OSError("tailscaled unavailable")

    settings = replace(PORTAL, locality_header="")
    peers = LocalityClassifier(
        settings, trusted_peers=frozenset({"127.0.0.1"}), runner=remote
    )
    decision = peers.classify(_headers({"tailscale-user-login": PARTNER}), "127.0.0.1")
    assert decision.locality is Locality.TAILNET
    assert jellyfin_destination(decision, ENDPOINTS) == TS_URL

    unavailable = LocalityClassifier(
        settings, trusted_peers=frozenset({"127.0.0.1"}), runner=broken
    )
    failed = unavailable.classify(
        _headers({"tailscale-user-login": PARTNER}), "127.0.0.1"
    )
    assert failed.locality is Locality.UNKNOWN
    assert jellyfin_destination(failed, ENDPOINTS) == TS_URL


def test_destination_is_always_one_of_two_configured_urls() -> None:
    for locality in Locality:
        decision = type("D", (), {"locality": locality, "source": "test"})()
        assert jellyfin_destination(decision, ENDPOINTS) in {LAN_URL, TS_URL}


# ------------------------------- security ------------------------------------


def test_portal_client_never_names_a_host_action_or_redirect() -> None:
    source = (
        Path(__file__).parents[1] / "src/butters/web/static/assets/portal.js"
    ).read_text()
    for forbidden in (
        "192.168.",
        "redirect=",
        "?host=",
        "local=true",
        "/api/admin/",
        "skill",
        "broker",
        "system.shutdown",
        "system.reboot",
        "delay:",
    ):
        assert forbidden not in source, forbidden
    # The one navigation it performs uses the server-supplied destination only.
    assert "window.location.assign(destination.destination)" in source
    assert source.count("window.location.assign") == 1
    # The destructive UI has no selector or option: confirmation freezes the
    # server-owned empty plan, then the browser completes FRESH WebAuthn.
    assert source.count('api("/api/portal/shutdown/plan"') == 1
    assert source.count('api("/api/portal/shutdown/authenticate/options"') == 1
    assert source.count('api("/api/portal/shutdown/authenticate/verify"') == 1
    assert "JSON.stringify({confirm:true})" in source
    assert "window.confirm(" in source
    document = (
        Path(__file__).parents[1] / "src/butters/web/static/portal.html"
    ).read_text()
    assert 'id="portal-shutdown"' in document
    assert "Shut Down NAS…" in document
    assert 'type="button" hidden' in document


def test_portal_service_exposes_no_generic_execution() -> None:
    source = (Path(__file__).parents[1] / "src/butters/web/portal.py").read_text()
    # The only frozen plans are the fixed wake and fixed NAS-Agent shutdown.
    assert source.count("self.runtime.actions.freeze(") == 2
    assert 'skill="wake_nas"' in source
    assert 'skill="nas.system.shutdown"' in source
    assert source.count("self.runtime.actions.execute(") == 2
    assert "shutdown_nas" not in source
    assert "skill=payload" not in source
    assert "skill=request" not in source
    # No desktop identifier of any kind is reachable from this module.
    for identifier in (
        "shutdown_desktop",
        "wake_desktop",
        "desktop.app",
        "desktop_agent",
        "start_admin_",
    ):
        assert identifier not in source, identifier


def test_portal_bandwidth_ui_preserves_unavailable_and_remote_only() -> None:
    root = Path(__file__).parents[1] / "src/butters/web/static"
    page = (root / "portal.html").read_text(encoding="utf-8")
    script = (root / "assets/portal.js").read_text(encoding="utf-8")
    assert 'id="portal-bandwidth"' in page
    assert 'id="portal-remote-streams"' in page
    assert 'id="portal-open"' in page
    assert '"Unavailable"' in script
    assert 'stream.classification!=="remote"' in script
    assert 'remote_jellyfin_stream_count===null?"unavailable"' in script
    assert 'unknown_stream_count===null?"unavailable"' in script
    assert 'measurement_quality==="unavailable"?"unavailable":"Stabilizing"' in script
    assert "if(state.jellyfin_ready){await enterJellyfin()" not in script
    assert "RemoteEndPoint" not in script
    assert "api_key" not in script


def test_portal_bandwidth_projection_excludes_protocol_and_local_details() -> None:
    common = {
        "user": "Viewer",
        "playing": True,
        "paused": False,
        "play_method": "direct_play",
        "observed_mbps": 8.0,
        "bitrate_source": "source_reported",
        "item": "Movie",
        "session_id": "stable-secret-id",
        "position_ticks": 42,
        "client": "Web",
        "device": "Phone",
    }
    projected = _portal_bandwidth(
        {
            "effective_capacity_mbps": 30.0,
            "sessions_above_target": ["stable-secret-id"],
            "remote_path_counters": {"derp": {"tx_bytes": 1}},
            "sessions": [
                {**common, "classification": "remote"},
                {**common, "classification": "local", "item": "Private local item"},
                {**common, "classification": "unknown", "item": "Unknown item"},
            ],
        }
    )
    assert projected is not None
    assert len(projected["sessions"]) == 1
    assert projected["sessions"][0]["classification"] == "remote"
    serialized = str(projected)
    assert "stable-secret-id" not in serialized
    assert "Private local item" not in serialized
    assert "Unknown item" not in serialized
    assert "remote_path_counters" not in projected
