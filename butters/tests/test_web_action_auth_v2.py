from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from pathlib import Path

import httpx
from butters.assistant_config import load_assistant_settings
from butters.auth.manager import AuthenticationVerification
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService


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


class Desktop:
    def __init__(self) -> None:
        self.calls = 0

    def status(self, machine):
        from butters.integrations.desktop import DesktopState

        return DesktopState(machine, True, True, True)

    def start_remote_session(self, machine, *, cancel_event=None):
        self.calls += 1
        return {
            "machine": machine,
            "wake_sent": False,
            "network_reachable": True,
            "ssh_ready": True,
            "remote_mode_requested": True,
            "parsec_ready": True,
            "verification_complete": True,
            "elapsed_ms": 1,
            "failed_stage": None,
            "error": None,
        }


def _application(tmp_path):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        desktop=replace(base.desktop, restart_enabled=True),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=("admin@example.com", "other@example.com"),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings,
        vocabulary,
        general_reasoner=NoCloud(),
        state_dir=tmp_path,
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
    implementation = service.assistant.skills.get(
        "start_remote_desktop_session"
    ).implementation.__self__
    desktop = Desktop()
    implementation.desktop = desktop
    return (
        create_app(settings, vocabulary, service, stt_engine_factory=Engine),
        service,
        desktop,
    )


def _credential() -> dict[str, object]:
    encoded = base64.urlsafe_b64encode(b"credential-one").rstrip(b"=").decode()
    return {"id": encoded, "uv": True}


async def _direct_action_freezes_then_passkey_resumes_exact_action(
    tmp_path,
) -> None:
    app, service, desktop = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            session = (await http.get("/api/session", headers=headers)).json()
            mutation = {
                **headers,
                "origin": "http://testserver",
                "x-butters-csrf": session["csrf_token"],
            }
            action = await http.post(
                "/api/chat",
                headers=mutation,
                json={"text": "Turn on my computer and get Parsec ready"},
            )
            payload = action.json()
            assert action.status_code == 200
            assert payload["route"] == "pending_auth"
            assert payload["authentication_required"] == "elevated"
            assert desktop.calls == 0
            pending = payload["pending_action"]["pending_action_id"]
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": pending},
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
            job = verified.json()["jobs"][0]
            for _ in range(100):
                observed = await http.get(
                    f"/api/actions/jobs/{job['job_id']}", headers=headers
                )
                if observed.json()["state"] in {"completed", "failed"}:
                    break
            assert observed.json()["state"] == "completed"
            assert desktop.calls == 1
            replay = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={
                    "ceremony_id": begin.json()["ceremony_id"],
                    "credential": _credential(),
                },
            )
            assert replay.status_code == 409
            assert replay.json()["error"] == "ceremony_replayed"
    finally:
        await app.state.shutdown_workers()
        del service


async def _elevation_does_not_satisfy_fresh_and_lock_is_immediate(tmp_path) -> None:
    app, service, _desktop = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            session = (await http.get("/api/session", headers=headers)).json()
            mutation = {
                **headers,
                "origin": "http://testserver",
                "x-butters-csrf": session["csrf_token"],
            }
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "elevation"},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={
                    "ceremony_id": begin.json()["ceremony_id"],
                    "credential": _credential(),
                },
            )
            assert verified.json()["status"]["elevated"] is True
            destructive = await http.post(
                "/api/chat",
                headers=mutation,
                json={"text": "Restart my desktop"},
            )
            assert destructive.json()["authentication_required"] == "fresh"
            locked = await http.post("/api/auth/lock", headers=mutation)
            assert locked.json()["elevated"] is False
    finally:
        await app.state.shutdown_workers()
        del service


async def _copied_browser_session_is_denied_for_another_tailnet_identity(
    tmp_path,
) -> None:
    app, service, _desktop = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            first = await http.get(
                "/api/session", headers={"tailscale-user-login": "admin@example.com"}
            )
            assert first.status_code == 200
            copied = await http.get(
                "/api/auth/status",
                headers={"tailscale-user-login": "other@example.com"},
            )
            assert copied.status_code == 403
            assert copied.json()["error"] == "session_identity_denied"
    finally:
        await app.state.shutdown_workers()
        del service


def test_direct_action_freezes_then_passkey_resumes_exact_action(tmp_path) -> None:
    asyncio.run(_direct_action_freezes_then_passkey_resumes_exact_action(tmp_path))


def test_elevation_does_not_satisfy_fresh_and_lock_is_immediate(tmp_path) -> None:
    asyncio.run(_elevation_does_not_satisfy_fresh_and_lock_is_immediate(tmp_path))


def test_copied_browser_session_is_denied_for_another_tailnet_identity(
    tmp_path,
) -> None:
    asyncio.run(
        _copied_browser_session_is_denied_for_another_tailnet_identity(tmp_path)
    )


def test_browser_auth_state_is_server_authoritative_and_not_persisted_locally() -> None:
    source = (
        Path(__file__).parents[1] / "src/butters/web/static/assets/app.js"
    ).read_text()
    assert 'api("/api/auth/status")' in source
    assert 'api("/api/auth/lock"' in source
    assert "butters.elevation" not in source
    assert source.count("localStorage.setItem") == 1
