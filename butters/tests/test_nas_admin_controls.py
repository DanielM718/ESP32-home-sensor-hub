"""Administrator NAS controls: wake, shutdown, gates, and argument rejection.

The invariants under test are structural. Neither endpoint has a request model
that can express a MAC, broadcast, IP, hostname, interface, shell, command,
executable, or argv, and neither can name a skill or broker operation. Shutdown
additionally requires FRESH authentication bound to the exact frozen action, an
explicit confirmation, and a broker gate that ships false.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace

import httpx
from butters.actions.broker import (
    BrokerOperation,
    FixedBrokerConfig,
    FixedBrokerOperations,
)
from butters.assistant_config import load_assistant_settings
from butters.auth.manager import AuthenticationVerification
from butters.integrations.model import IntegrationError
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import CONVERSATIONAL_PLANNER_ACTIONS, BetaAssistantService


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


class FakeWebAuthn:
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
    """Stands in for the adapter; records what the skill actually asked for."""

    def __init__(self, *, shutdown_enabled: bool = True) -> None:
        self.wakes = 0
        self.shutdowns = 0
        self.shutdown_enabled = shutdown_enabled

    def wake(self, _cancel=None):
        self.wakes += 1
        return {"accepted": True, "outcome": "wake_packet_sent"}

    def shutdown(self, _cancel=None):
        if not self.shutdown_enabled:
            raise IntegrationError(
                "capability_unavailable", "NAS shutdown is not configured"
            )
        self.shutdowns += 1
        return {"accepted": True, "outcome": "shutdown_requested"}


def _application(tmp_path, *, shutdown_available: bool = True):
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
                enabled=shutdown_available,
                configured=shutdown_available,
            ),
        ).validated(),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=("admin@example.com",),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings, vocabulary, general_reasoner=NoCloud(), state_dir=tmp_path
    )
    service.passkeys.backend = FakeWebAuthn()
    service.auth_state.add_credential(
        credential_id=b"credential-one",
        public_key=b"public",
        user_id=b"user",
        identity="identity:admin@example.com",
        label="Phone",
        sign_count=0,
        device_type="multi_device",
        backed_up=True,
    )
    nas = FakeNas()
    service.assistant.skills.get("wake_nas").implementation.__self__.nas = nas
    return create_app(settings, vocabulary, service, stt_engine_factory=Engine), service, nas


def _credential() -> dict[str, object]:
    encoded = base64.urlsafe_b64encode(b"credential-one").rstrip(b"=").decode()
    return {"id": encoded, "uv": True}


async def _session(http, headers):
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _await_job(http, headers, job_id):
    for _ in range(200):
        observed = await http.get(f"/api/actions/jobs/{job_id}", headers=headers)
        if observed.json()["state"] in {"completed", "failed", "cancelled"}:
            return observed.json()
        await asyncio.sleep(0.01)
    raise AssertionError("job never settled")


async def _elevate(http, mutation):
    begin = await http.post(
        "/api/auth/authenticate/options", headers=mutation, json={"purpose": "elevation"}
    )
    await http.post(
        "/api/auth/authenticate/verify",
        headers=mutation,
        json={"ceremony_id": begin.json()["ceremony_id"], "credential": _credential()},
    )


# ------------------------------- Wake NAS -----------------------------------


async def _wake_accepts_only_an_empty_object(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _session(http, headers)
            # Every shape a caller might use to smuggle a target is refused,
            # before any authentication or broker work happens.
            for payload in (
                {"mac": "00:e2:69:7d:40:cd"},
                {"broadcast": "192.168.1.255"},
                {"ip": "192.168.1.50"},
                {"host": "nas.local"},
                {"hostname": "nas"},
                {"interface": "enp6s0"},
                {"shell": "/bin/sh"},
                {"command": "poweroff"},
                {"executable": "/usr/bin/wakeonlan"},
                {"argv": ["/usr/bin/wakeonlan"]},
                {"machine": "nas"},
                {"skill": "shutdown_nas"},
            ):
                response = await http.post(
                    "/api/admin/tools/wake-nas", headers=mutation, json=payload
                )
                assert response.status_code == 400, payload
                assert nas.wakes == 0

            await _elevate(http, mutation)
            accepted = await http.post(
                "/api/admin/tools/wake-nas", headers=mutation, json={}
            )
            assert accepted.status_code == 200
            body = accepted.json()
            assert body["status"] == "queued"
            job = await _await_job(http, headers, body["jobs"][0]["job_id"])
            assert job["state"] == "completed"
            assert nas.wakes == 1
    finally:
        await app.state.shutdown_workers()
        del service


async def _wake_requires_administrator_and_authentication(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        # A separate client per identity: a browser session is bound to the
        # identity that created it, so they must not share a cookie jar.
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anonymous_http:
            anonymous = await _session(anonymous_http, {})
            denied = await anonymous_http.post(
                "/api/admin/tools/wake-nas", headers=anonymous, json={}
            )
            assert denied.status_code in {401, 403}
            assert nas.wakes == 0

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            headers = {"tailscale-user-login": "admin@example.com"}
            mutation = await _session(http, headers)
            pending = await http.post(
                "/api/admin/tools/wake-nas", headers=mutation, json={}
            )
            assert pending.status_code == 200
            assert pending.json()["status"] == "authentication_required"
            assert pending.json()["authentication_required"] == "elevated"
            assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


async def _wake_result_claims_only_that_a_packet_was_sent(tmp_path) -> None:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _session(http, headers)
            await _elevate(http, mutation)
            body = (
                await http.post("/api/admin/tools/wake-nas", headers=mutation, json={})
            ).json()
            job = await _await_job(http, headers, body["jobs"][0]["job_id"])
            serialized = repr(job).lower()
            assert "wake_packet_sent" in serialized
            for claim in ("booted", "online", "powered on", "nas is up"):
                assert claim not in serialized
            status = (
                await http.get("/api/admin/tools/nas", headers=headers)
            ).json()
            assert status["last_operation"]["operation"] == "wake_nas"
            # The record is history; it is not the observed aggregate.
            assert status["last_operation"]["outcome"] != status["aggregate"]
    finally:
        await app.state.shutdown_workers()
        del service


# ----------------------------- Shutdown NAS ---------------------------------


async def _shutdown_requires_confirmation_fresh_auth_and_empty_body(tmp_path) -> None:
    app, service, nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _session(http, headers)
            for payload in (
                {},
                {"confirm": False},
                {"confirm": "yes"},
                {"confirm": True, "ip": "192.168.1.50"},
                {"host": "nas.local"},
                {"username": "root"},
                {"command": "poweroff"},
                {"shell": "/bin/sh"},
                {"argv": ["poweroff"]},
                {"api_url": "https://nas.local"},
                {"api_token": "secret"},
                {"arguments": "-h now"},
            ):
                response = await http.post(
                    "/api/admin/tools/shutdown-nas", headers=mutation, json=payload
                )
                assert response.status_code in {400, 403}, payload
                assert nas.shutdowns == 0

            # A live elevation is deliberately not enough for a FRESH action.
            await _elevate(http, mutation)
            pending = await http.post(
                "/api/admin/tools/shutdown-nas",
                headers=mutation,
                json={"confirm": True},
            )
            assert pending.status_code == 200
            body = pending.json()
            assert body["status"] == "authentication_required"
            assert body["authentication_required"] == "fresh"
            assert nas.shutdowns == 0

            plan = body["pending_action"]["pending_action_id"]
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={
                    "ceremony_id": begin.json()["ceremony_id"],
                    "credential": _credential(),
                },
            )
            assert verified.status_code == 200
            job = await _await_job(
                http, headers, verified.json()["jobs"][0]["job_id"]
            )
            assert job["state"] == "completed"
            assert nas.shutdowns == 1
    finally:
        await app.state.shutdown_workers()
        del service


async def _shutdown_is_unavailable_when_not_configured(tmp_path) -> None:
    app, service, nas = _application(tmp_path, shutdown_available=False)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _session(http, headers)
            response = await http.post(
                "/api/admin/tools/shutdown-nas",
                headers=mutation,
                json={"confirm": True},
            )
            assert response.status_code in {400, 403, 409}
            assert response.json()["error"] == "capability_unavailable"
            assert nas.shutdowns == 0
            status = (
                await http.get("/api/admin/tools/nas", headers=headers)
            ).json()
            assert status["capability"]["shutdown_configured"] is False
    finally:
        await app.state.shutdown_workers()
        del service


def test_wake_accepts_only_an_empty_object(tmp_path) -> None:
    asyncio.run(_wake_accepts_only_an_empty_object(tmp_path))


def test_wake_requires_administrator_and_authentication(tmp_path) -> None:
    asyncio.run(_wake_requires_administrator_and_authentication(tmp_path))


def test_wake_result_claims_only_that_a_packet_was_sent(tmp_path) -> None:
    asyncio.run(_wake_result_claims_only_that_a_packet_was_sent(tmp_path))


def test_shutdown_requires_confirmation_fresh_auth_and_empty_body(tmp_path) -> None:
    asyncio.run(_shutdown_requires_confirmation_fresh_auth_and_empty_body(tmp_path))


def test_shutdown_is_unavailable_when_not_configured(tmp_path) -> None:
    asyncio.run(_shutdown_is_unavailable_when_not_configured(tmp_path))


# ------------------------- structural / gate tests --------------------------


def test_registered_nas_actions_take_no_parameters() -> None:
    from butters.assistant_config import (
        ActionSettings,
        BrokerSettings,
        DesktopSettings,
        KnownDeviceSettings,
    )
    from butters.skills.actions_v2 import register_action_skills
    from butters.skills.registry import SkillRegistry

    registry = SkillRegistry()
    enabled = KnownDeviceSettings(enabled=True, configured=True, maximum_duration_minutes=1)
    register_action_skills(
        registry,
        desktop=DesktopSettings(),
        broker=BrokerSettings(enabled=True),
        actions=ActionSettings(nas=enabled, nas_shutdown=enabled).validated(),
        host=object(),
        action_adapter=object(),
        nas=object(),
        environment=object(),
    )
    for name in ("wake_nas", "shutdown_nas"):
        spec = registry.get(name)
        assert spec is not None
        assert spec.input_schema == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    assert registry.get("wake_nas").authentication.value == "elevated"
    shutdown = registry.get("shutdown_nas")
    assert shutdown.authentication.value == "fresh"
    assert shutdown.explicit_intent_required is True
    assert shutdown.confirmation_required is True
    # Powering the NAS off is never a local-console convenience.
    assert shutdown.local_console_allowed is False


def test_nas_shutdown_broker_gate_ships_false_and_needs_a_fixed_transport() -> None:
    from pathlib import Path

    example = (
        Path(__file__).parents[1] / "config/action-broker.example.toml"
    ).read_text()
    assert '"nas.shutdown" = false' in example
    assert '"nas.wake" = false' in example

    # Without a configured URL and credential the handler is not registered at
    # all, so the operation cannot be dispatched even if its gate were flipped.
    config = FixedBrokerConfig(
        desktop_host="d",
        desktop_user="u",
        desktop_mac="00:11:22:33:44:55",
        desktop_broadcast="192.168.1.255",
        desktop_key=Path("/etc/butters/action-broker/key"),
        nas_mac="00:e2:69:7d:40:cd",
        nas_broadcast="192.168.1.255",
        enabled_operations=frozenset(
            {BrokerOperation.NAS_WAKE, BrokerOperation.NAS_SHUTDOWN}
        ),
    )
    assert BrokerOperation.NAS_SHUTDOWN not in FixedBrokerOperations(config).handlers()
    configured = FixedBrokerOperations(
        replace(config, nas_api_url="https://nas.local"), nas_api_key="token"
    )
    assert BrokerOperation.NAS_SHUTDOWN in configured.handlers()


def test_nas_shutdown_transport_sends_one_fixed_request_with_no_caller_input() -> None:
    from pathlib import Path

    sent = []

    class _Response:
        status = 200

        def __init__(self) -> None:
            self._remaining = b"1"

        def read(self, _size):
            value, self._remaining = self._remaining, b""
            return value

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def opener(request, timeout=None):
        sent.append((request.full_url, request.get_method(), request.data))
        return _Response()

    operations = FixedBrokerOperations(
        FixedBrokerConfig(
            desktop_host="d",
            desktop_user="u",
            desktop_mac="00:11:22:33:44:55",
            desktop_broadcast="192.168.1.255",
            desktop_key=Path("/etc/butters/action-broker/key"),
            nas_api_url="https://nas.local",
            enabled_operations=frozenset({BrokerOperation.NAS_SHUTDOWN}),
        ),
        nas_api_key="token",
        opener=opener,
    )
    result = operations.nas_shutdown()
    assert result["accepted"] is True
    assert result["transport"] == "truenas_api"
    assert sent == [("https://nas.local/api/v2.0/system/shutdown", "POST", b"{}")]
    # The handler takes no argument at all, so nothing caller-supplied exists.
    import inspect

    assert not inspect.signature(operations.nas_shutdown).parameters


def test_nas_actions_are_absent_from_every_planner_catalog() -> None:
    assert "wake_nas" not in CONVERSATIONAL_PLANNER_ACTIONS
    assert "shutdown_nas" not in CONVERSATIONAL_PLANNER_ACTIONS
