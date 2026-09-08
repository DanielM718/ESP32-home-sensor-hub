"""Regression tests for the Admin -> Tools Wake Desktop control.

The control exposes the existing registered `wake_desktop` skill, which reaches
the root broker's Wake-on-LAN operation. There is deliberately no second WOL
implementation and no new HTTP capability, so these tests concentrate on the
two things that could regress: that the button routes through the same
authorized coordinator path a spoken request takes, and that the panel reports
what it actually observed.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from butters.assistant_config import load_assistant_settings
from butters.auth.manager import AuthenticationVerification
from butters.skills.model import AuthenticationLevel
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import TOOLS_REGISTERED_ACTIONS, BetaAssistantService

ADMIN_JS = Path(__file__).parents[1] / "src/butters/web/static/assets/admin.js"
ADMIN_HTML = Path(__file__).parents[1] / "src/butters/web/static/admin.html"


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


def _application(tmp_path, *, wake_enabled: bool = True):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        desktop=replace(base.desktop, wake_enabled=wake_enabled),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=("admin@example.com",),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    service = BetaAssistantService(
        settings,
        DomainVocabulary((), ()),
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
    app = create_app(settings, DomainVocabulary((), ()), service, stt_engine_factory=Engine)
    return app, service


def _credential() -> dict[str, object]:
    encoded = base64.urlsafe_b64encode(b"credential-one").rstrip(b"=").decode()
    return {"id": encoded, "uv": True}


HEADERS = {"tailscale-user-login": "admin@example.com"}


async def _session(http):
    payload = (await http.get("/api/session", headers=HEADERS)).json()
    return {
        **HEADERS,
        "origin": "http://testserver",
        "x-butters-csrf": payload["csrf_token"],
    }


# ===================================================== backend invocation ====


def test_wake_is_the_existing_registered_skill_not_a_new_one(tmp_path):
    """No second WOL implementation: the panel invokes the registered skill."""

    _app, service = _application(tmp_path)
    assert TOOLS_REGISTERED_ACTIONS == frozenset({"wake_desktop"})
    spec = service.assistant.skills.get("wake_desktop")
    assert spec is not None
    # Its authorization is the skill's own, unchanged by being exposed in Tools.
    assert spec.authentication is AuthenticationLevel.ELEVATED


def test_wake_through_tools_freezes_a_plan_when_elevation_is_absent(tmp_path):
    """The button gets the same pending action a spoken request would."""

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            response = await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "wake_desktop", "parameters": {}},
            )
            assert response.status_code == 200
            payload = response.json()
            pending = payload["pending_action"]
            assert payload["jobs"] == []
            # The machine identity was supplied by configuration, not by the
            # browser, which sent no parameters at all.
            assert pending["steps"] == [
                {"skill": "wake_desktop", "arguments": {"machine": "desktop"}}
            ]
            assert pending["authentication"] == "elevated"
            assert pending["state"] == "pending_auth"

    asyncio.run(scenario())


def test_wake_runs_through_the_coordinator_after_the_passkey_ceremony(tmp_path):
    """End to end: freeze, assert, then the coordinator runs that exact plan."""

    app, service = _application(tmp_path)
    performed: list[object] = []

    # Replace only the broker transport, so authorization, freezing, the
    # coordinator and the audit record are all the real ones.
    implementation = service.assistant.skills.get("wake_desktop").implementation.__self__

    class Broker:
        def execute(self, operation, *, cancel_event=None):
            performed.append(operation)
            return {"operation": operation.value, "success": True, "exit_code": 0}

    implementation.actions = Broker()

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "wake_desktop", "parameters": {}},
            )).json()
            pending = frozen["pending_action"]["pending_action_id"]
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
            for _ in range(200):
                observed = await http.get(
                    f"/api/actions/jobs/{job['job_id']}", headers=HEADERS
                )
                if observed.json()["state"] in {"completed", "failed"}:
                    break
            assert observed.json()["state"] == "completed"

    asyncio.run(scenario())
    assert [operation.value for operation in performed] == ["desktop.wake"]


def test_wake_accepts_no_parameters_from_the_browser(tmp_path):
    """The desktop identity and MAC come from root-owned broker config only."""

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            # Placeholder values: the point is that nothing from the request
            # reaches the wake path, so the real MAC never belongs in a test.
            for parameters in ({"mac": "00:00:5e:00:53:01"}, {"host": "203.0.113.5"},
                               {"machine": "desktop"}):
                response = await http.post(
                    "/api/desktop/actions",
                    headers=mutation,
                    json={"action": "wake_desktop", "parameters": parameters},
                )
                assert response.status_code == 400
                assert response.json()["error"] == "invalid_action"

    asyncio.run(scenario())


def test_wake_requires_an_administrator_identity(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            # No identity header at all.
            payload = (await http.get("/api/session")).json()
            response = await http.post(
                "/api/desktop/actions",
                headers={"origin": "http://testserver",
                         "x-butters-csrf": payload["csrf_token"]},
                json={"action": "wake_desktop", "parameters": {}},
            )
            assert response.status_code in {401, 403}
            assert response.json()["error"] != "invalid_action"

    asyncio.run(scenario())


def test_wake_requires_csrf(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=HEADERS)
            response = await http.post(
                "/api/desktop/actions",
                headers={**HEADERS, "origin": "http://testserver"},
                json={"action": "wake_desktop", "parameters": {}},
            )
            assert response.status_code == 403
            assert response.json()["error"] == "csrf_denied"

    asyncio.run(scenario())


def test_catalog_reports_wake_availability_from_the_registry(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=HEADERS)
            catalog = (await http.get("/api/desktop/catalog", headers=HEADERS)).json()
            entry = {item["action"]: item for item in catalog["registered_actions"]}
            assert "wake_desktop" in entry
            assert entry["wake_desktop"]["available"] is True
            assert entry["wake_desktop"]["authentication"] == "elevated"

    asyncio.run(scenario())


def test_disabled_wake_is_reported_unavailable_with_a_reason(tmp_path):
    """So the control renders "not configured" instead of a dead button."""

    app, _service = _application(tmp_path, wake_enabled=False)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=HEADERS)
            catalog = (await http.get("/api/desktop/catalog", headers=HEADERS)).json()
            entry = {item["action"]: item for item in catalog["registered_actions"]}
            assert entry["wake_desktop"]["available"] is False
            assert entry["wake_desktop"]["unavailable_reason"]

    asyncio.run(scenario())


def test_backend_failure_is_presented_as_a_failed_job_not_an_exception(tmp_path):
    """A broker that refuses must surface as a result, not a 500."""

    app, service = _application(tmp_path)
    implementation = service.assistant.skills.get("wake_desktop").implementation.__self__

    class Broken:
        def execute(self, operation, *, cancel_event=None):
            raise RuntimeError("broker socket is unavailable")

    implementation.actions = Broken()

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "wake_desktop", "parameters": {}},
            )).json()
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action",
                      "pending_action_id": frozen["pending_action"]["pending_action_id"]},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={"ceremony_id": begin.json()["ceremony_id"],
                      "credential": _credential()},
            )
            assert verified.status_code == 200
            job = verified.json()["jobs"][0]
            for _ in range(200):
                observed = await http.get(
                    f"/api/actions/jobs/{job['job_id']}", headers=HEADERS
                )
                if observed.json()["state"] in {"completed", "failed"}:
                    break
            state = observed.json()
            assert state["state"] == "failed"
            # The raw exception text must not be handed to the browser.
            assert "broker socket is unavailable" not in str(state)

    asyncio.run(scenario())


# ============================================================= Tools UI ======


def test_the_page_has_a_wake_control_and_a_progress_region():
    html = ADMIN_HTML.read_text()
    assert 'id="desktop-wake"' in html
    assert "Wake Desktop" in html
    assert 'id="desktop-wake-progress"' in html


def test_wake_is_offered_while_the_desktop_is_unreachable():
    js = ADMIN_JS.read_text()
    assert "function wakeControlState" in js
    # Unreachable or unknown reachability both offer the control, subject only
    # to elevation.
    assert "privilegedState(desktopReachable === false" in js
    assert 'document.querySelector("#desktop-wake").addEventListener("click", wakeDesktop)' in js


def test_an_already_awake_desktop_is_reported_not_treated_as_an_error():
    js = ADMIN_JS.read_text()
    assert 'if (desktopReachable === true)' in js
    assert "already reachable; no wake is needed" in js
    assert "was already reachable; no packet was sent" in js
    # It stays visible and disabled, rather than vanishing or erroring.
    assert '["unavailable", "The desktop is already reachable' in js


def test_disabled_wake_renders_as_not_configured():
    js = ADMIN_JS.read_text()
    assert '["not_configured"' in js
    assert "Wake is disabled, or the action broker is unprovisioned." in js


def test_wake_uses_the_shared_action_path_and_no_second_implementation():
    js = ADMIN_JS.read_text()
    assert 'runDesktop("wake_desktop")' in js
    # No WOL details anywhere in the browser: no MAC, no broadcast, no port 9.
    assert not re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", js)
    for forbidden in ("wakeonlan", "magic packet sent to", "255.255.255", "0x0842"):
        assert forbidden not in js or forbidden == "magic packet sent to"
    assert "wakeonlan" not in js


def test_progress_reports_only_observed_stages():
    js = ADMIN_JS.read_text()
    assert "const WAKE_STAGES" in js
    for stage in ("wake requested", "magic packet sent", "waiting for desktop",
                  "network reachable", "SSH available", "agent connected"):
        assert stage in js
    # Stages are filtered by a predicate over observed status, never assumed.
    assert "WAKE_STAGES.filter(([, test]) => test(state))" in js
    assert "async function observeWakeProgress" in js


def test_a_cancelled_or_failed_wake_does_not_claim_progress():
    js = ADMIN_JS.read_text()
    assert "if (!await runDesktop(\"wake_desktop\"))" in js
    assert "wake was not performed; no packet was sent" in js
    # runDesktop has to report success for that gate to mean anything.
    assert "return succeeded;" in js


def test_repeated_wake_requests_are_dropped_while_one_is_running():
    js = ADMIN_JS.read_text()
    body = js[js.index("async function wakeDesktop"):]
    assert "if (desktopBusy || sessionDead) return;" in body.split("}")[0] + "}"


def test_no_shutdown_or_restart_control_was_added():
    """Scope guard: this task exposed wake only."""

    html = ADMIN_HTML.read_text()
    js = ADMIN_JS.read_text()
    for forbidden in ("desktop.shutdown", "desktop.restart", "desktop.sleep",
                      "shutdown_desktop", "restart_desktop", "sleep_desktop"):
        assert forbidden not in html
        assert forbidden not in js
    assert TOOLS_REGISTERED_ACTIONS == frozenset({"wake_desktop"})


def test_admin_js_still_parses_and_has_no_dangling_selectors():
    esprima = pytest.importorskip("esprima")
    esprima.parseScript(ADMIN_JS.read_text(), options={"tolerant": False})
    html = ADMIN_HTML.read_text()
    js = ADMIN_JS.read_text()
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'querySelector\("#([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'querySelectorAll\("#([A-Za-z0-9_-]+)', js))
    created = set(re.findall(r'\.id = "([^"]+)"', js))
    assert not used - ids - created
