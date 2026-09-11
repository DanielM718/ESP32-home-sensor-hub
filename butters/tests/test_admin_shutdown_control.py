"""Regression tests for the Admin -> Tools Shut Down Desktop control.

Shutdown reuses the `shutdown_desktop` skill that already existed, which
reaches the root broker's one fixed `desktop.shutdown` operation. Nothing here
is a second implementation, so these tests concentrate on the properties that
make exposing it safe: it is FRESH-authenticated rather than merely elevated,
it is always frozen for an explicit confirmation before anything runs, the
browser cannot influence which machine is shut down, and the panel reports what
it observed rather than assuming the machine went away.
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
from butters.web.service import (
    TOOLS_CONFIRM_ACTIONS,
    TOOLS_REGISTERED_ACTIONS,
    BetaAssistantService,
)

ADMIN_JS = Path(__file__).parents[1] / "src/butters/web/static/assets/admin.js"
ADMIN_HTML = Path(__file__).parents[1] / "src/butters/web/static/admin.html"
ADMIN_CSS = Path(__file__).parents[1] / "src/butters/web/static/assets/styles.css"


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


def _application(tmp_path, *, shutdown_enabled: bool = True):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        desktop=replace(
            base.desktop, wake_enabled=True, shutdown_enabled=shutdown_enabled
        ),
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


def _install_broker(service, *, failing: bool = False):
    """Replace only the broker transport, keeping every authorization layer."""

    performed: list[object] = []
    implementation = service.assistant.skills.get("shutdown_desktop").implementation.__self__

    class Broker:
        def execute(self, operation, *, cancel_event=None):
            performed.append(operation)
            if failing:
                raise RuntimeError("fixed operation failed")
            return {
                "operation": operation.value,
                "accepted": True,
                "transition": "shutdown",
                "scheduled": True,
            }

    implementation.actions = Broker()
    return performed


async def _drain(http, job):
    for _ in range(300):
        observed = await http.get(f"/api/actions/jobs/{job['job_id']}", headers=HEADERS)
        state = observed.json()["state"]
        if state in {"completed", "failed", "cancelled"}:
            return observed.json()
        await asyncio.sleep(0.01)
    raise AssertionError(f"job never settled: {job['job_id']}")


# ================================================== registration and policy ===


def test_shutdown_is_the_existing_registered_skill_not_a_new_one(tmp_path):
    _app, service = _application(tmp_path)
    assert "shutdown_desktop" in TOOLS_REGISTERED_ACTIONS
    spec = service.assistant.skills.get("shutdown_desktop")
    assert spec is not None
    # Higher risk than wake, and it says so in the only place that matters:
    # wake is ELEVATED, shutdown is FRESH, which no standing elevation can
    # satisfy on its own.
    assert spec.authentication is AuthenticationLevel.FRESH
    assert service.assistant.skills.get("wake_desktop").authentication is (
        AuthenticationLevel.ELEVATED
    )
    assert spec.confirmation_required is True


def test_shutdown_is_the_confirmed_subset_of_the_tools_actions():
    assert TOOLS_CONFIRM_ACTIONS == frozenset({"shutdown_desktop"})
    assert TOOLS_CONFIRM_ACTIONS <= TOOLS_REGISTERED_ACTIONS
    # Wake stays a single authorization, not a confirmation ceremony.
    assert "wake_desktop" not in TOOLS_CONFIRM_ACTIONS


def test_a_disabled_shutdown_is_reported_unavailable_with_a_reason(tmp_path):
    """Turning the capability off must disable the control, not hide it."""

    _app, service = _application(tmp_path, shutdown_enabled=False)
    entry = next(
        item
        for item in service.tools_registered_actions()
        if item["action"] == "shutdown_desktop"
    )
    assert entry["available"] is False
    assert entry["unavailable_reason"]


def test_the_catalog_reports_shutdown_availability_from_the_registry(tmp_path):
    _app, service = _application(tmp_path)
    entry = next(
        item
        for item in service.tools_registered_actions()
        if item["action"] == "shutdown_desktop"
    )
    assert entry["available"] is True
    assert entry["authentication"] == "fresh"


# ===================================================== freezing and consent ===


def test_shutdown_freezes_a_plan_in_the_confirmation_state(tmp_path):
    """The panel gets a frozen plan to confirm, never an immediate run."""

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            payload = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            assert payload["jobs"] == []
            pending = payload["pending_action"]
            # The confirmation state is what makes the coordinator audit the
            # run as a confirmed request rather than a direct one.
            assert pending["state"] == "pending_confirmation"
            assert pending["authentication"] == "fresh"
            # The machine came from configuration; the browser sent nothing.
            assert pending["steps"] == [
                {"skill": "shutdown_desktop", "arguments": {"machine": "desktop"}}
            ]

    asyncio.run(scenario())


def test_standing_elevation_does_not_skip_the_shutdown_confirmation(tmp_path):
    """An already-elevated session still gets a frozen plan.

    This is the property that separates shutdown from wake: elevation is enough
    to run wake on one click, and is deliberately never enough for shutdown.
    """

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "elevation"},
            )
            elevated = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={"ceremony_id": begin.json()["ceremony_id"],
                      "credential": _credential()},
            )
            assert elevated.json()["status"]["elevated"] is True

            # Wake, with the same elevation, runs straight away.
            woken = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "wake_desktop", "parameters": {}},
            )).json()
            assert woken.get("jobs")

            # Shutdown does not.
            payload = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            assert payload["jobs"] == []
            assert payload["pending_action"]["state"] == "pending_confirmation"

    asyncio.run(scenario())


def test_shutdown_accepts_no_parameters_from_the_browser(tmp_path):
    """No host, address, MAC or command line can come from the page."""

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            # Documentation-range placeholders: the point is that none of them
            # reaches the shutdown path at all.
            for parameters in ({"machine": "other-desktop"}, {"host": "203.0.113.5"},
                               {"mac": "00:00:5e:00:53:01"}, {"force": True},
                               {"command": "shutdown.exe /s /f /t 0"}):
                response = await http.post(
                    "/api/desktop/actions",
                    headers=mutation,
                    json={"action": "shutdown_desktop", "parameters": parameters},
                )
                assert response.status_code == 400
                assert response.json()["error"] == "invalid_action"

    asyncio.run(scenario())


def test_declining_the_confirmation_releases_the_frozen_plan(tmp_path):
    """Cancel uses the existing endpoint, and the released plan cannot run."""

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            plan = frozen["pending_action"]["pending_action_id"]
            cancelled = await http.post(
                f"/api/actions/pending/{plan}/cancel", headers=mutation
            )
            assert cancelled.status_code in {200, 204}
            # The released plan is no longer a thing a passkey can satisfy.
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            assert begin.status_code >= 400

    asyncio.run(scenario())


# ============================================================== execution ====


def test_shutdown_runs_through_the_coordinator_after_the_passkey_ceremony(tmp_path):
    """Freeze, confirm, assert, and only then does the broker see one call."""

    app, service = _application(tmp_path)
    performed = _install_broker(service)
    authorizations: list[object] = []
    registry = service.actions.registry
    original = registry.execute

    def recording(skill, arguments, **kwargs):
        authorizations.append(kwargs.get("action_authorization"))
        return original(skill, arguments, **kwargs)

    registry.execute = recording

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            plan = frozen["pending_action"]["pending_action_id"]
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={"ceremony_id": begin.json()["ceremony_id"],
                      "credential": _credential()},
            )
            assert verified.status_code == 200
            return await _drain(http, verified.json()["jobs"][0])

    job = asyncio.run(scenario())
    assert job["state"] == "completed"
    assert [operation.value for operation in performed] == ["desktop.shutdown"]
    # The audit reason distinguishes a confirmed action from a direct one.
    assert authorizations and authorizations[0].source == "confirmed_user_request"
    assert authorizations[0].confirmed is True


def test_a_broker_failure_is_a_failed_job_not_an_exception(tmp_path):
    """A desktop that refuses the operation must not take the panel down."""

    app, service = _application(tmp_path)
    performed = _install_broker(service, failing=True)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            plan = frozen["pending_action"]["pending_action_id"]
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={"ceremony_id": begin.json()["ceremony_id"],
                      "credential": _credential()},
            )
            return await _drain(http, verified.json()["jobs"][0])

    job = asyncio.run(scenario())
    assert job["state"] == "failed"
    assert job["failure_code"]
    assert performed  # the broker was reached; the failure is the desktop's


def test_a_frozen_shutdown_plan_is_single_use(tmp_path):
    """Replaying the ceremony cannot shut the desktop down a second time."""

    app, service = _application(tmp_path)
    performed = _install_broker(service)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            mutation = await _session(http)
            frozen = (await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "shutdown_desktop", "parameters": {}},
            )).json()
            plan = frozen["pending_action"]["pending_action_id"]
            begin = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            verified = await http.post(
                "/api/auth/authenticate/verify",
                headers=mutation,
                json={"ceremony_id": begin.json()["ceremony_id"],
                      "credential": _credential()},
            )
            await _drain(http, verified.json()["jobs"][0])
            # The plan has been claimed; a second ceremony against it is denied.
            replay = await http.post(
                "/api/auth/authenticate/options",
                headers=mutation,
                json={"purpose": "pending_action", "pending_action_id": plan},
            )
            assert replay.status_code >= 400

    asyncio.run(scenario())
    assert len(performed) == 1


def test_shutdown_requires_an_administrator_identity(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            headers = {"tailscale-user-login": "someone-else@example.com"}
            payload = (await http.get("/api/session", headers=headers)).json()
            response = await http.post(
                "/api/desktop/actions",
                headers={**headers, "origin": "http://testserver",
                         "x-butters-csrf": payload["csrf_token"]},
                json={"action": "shutdown_desktop", "parameters": {}},
            )
            assert response.status_code in {401, 403}

    asyncio.run(scenario())


def test_shutdown_requires_csrf(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=HEADERS)
            response = await http.post(
                "/api/desktop/actions",
                headers={**HEADERS, "origin": "http://testserver"},
                json={"action": "shutdown_desktop", "parameters": {}},
            )
            assert response.status_code in {400, 403}

    asyncio.run(scenario())


# ================================================================ the page ===


def test_the_page_has_a_shutdown_control_and_a_progress_region():
    html = ADMIN_HTML.read_text()
    assert 'id="desktop-shutdown"' in html
    assert 'id="desktop-shutdown-progress"' in html
    assert 'id="desktop-shutdown-confirm"' in html


def test_shutdown_is_not_adjacent_to_wake():
    """An accidental click meant for Wake must not be able to land on it."""

    html = ADMIN_HTML.read_text()
    wake = html.index('id="desktop-wake"')
    shutdown = html.index('id="desktop-shutdown"')
    assert shutdown > wake
    # Not merely later in the document: in a separate card, with the Compute
    # section and the result panel in between.
    between = html[wake:shutdown]
    assert "danger-card" in between
    assert "Compute" in between
    assert 'id="desktop-result-summary"' in between


def test_shutdown_is_visually_distinct_but_not_by_colour_alone():
    html = ADMIN_HTML.read_text()
    css = ADMIN_CSS.read_text()
    assert "danger-button" in html
    assert "risk-tag" in html
    # The word carries the risk, so the styling is not the only signal.
    assert "Higher risk" in html
    assert ".danger-card" in css
    assert ".danger-button" in css


def test_shutdown_uses_the_shared_action_path_and_no_second_implementation():
    js = ADMIN_JS.read_text()
    assert 'runDesktop("shutdown_desktop"' in js
    # No command line, no host, no address anywhere in the browser.
    for forbidden in ("shutdown.exe", "/s /t", "poweroff", "desktop-control.ps1"):
        assert forbidden not in js
    assert not re.search(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", js)


def test_the_confirmation_is_the_frozen_plan_not_a_browser_confirm():
    js = ADMIN_JS.read_text()
    assert "function confirmShutdown" in js
    # It is handed the plan the backend froze, and shows what it is confirming.
    assert "plan.steps" in js
    assert "pending_confirmation" in js
    # Declining releases that plan through the existing endpoint.
    assert "async function cancelPendingAction" in js
    assert "/api/actions/pending/" in js
    assert "cancelPendingAction(result.pending_action.pending_action_id)" in js
    # Not a window.confirm bypass. Scoped to the Tools panel, because the
    # Passkeys panel has its own long-standing confirm() for revocation.
    tools = js[js.index("Tools panel ==="):]
    for forbidden in ("window.confirm", "if (confirm(", "!confirm("):
        assert forbidden not in tools


def test_repeated_shutdown_requests_are_dropped_while_one_is_running():
    js = ADMIN_JS.read_text()
    body = js[js.index("async function shutdownDesktop"):]
    assert "if (desktopBusy || sessionDead) return;" in body.split("}")[0] + "}"


def test_an_already_offline_desktop_is_reported_not_treated_as_a_failure():
    js = ADMIN_JS.read_text()
    assert "Desktop already offline" in js
    assert '["unavailable", "The desktop is already offline' in js


def test_shutdown_progress_reports_only_observed_stages():
    js = ADMIN_JS.read_text()
    assert "const SHUTDOWN_STAGES" in js
    for stage in ("shutdown requested", "waiting for desktop to go offline",
                  "desktop offline"):
        assert stage in js
    assert "async function observeShutdownProgress" in js
    # Offline is asserted only from an observed status, never assumed.
    assert "if (status.online === false)" in js


def test_a_still_reachable_desktop_is_not_reported_as_a_completed_shutdown():
    js = ADMIN_JS.read_text()
    assert "still reachable and has not confirmed it went offline" in js
    # And that outcome is not dressed up as a success tick.
    window = js[js.index("async function observeShutdownProgress"):]
    tail = window[window.index("still reachable and has not gone offline yet"):]
    assert "showResult({ok: null," in tail


def test_a_cancelled_or_failed_shutdown_does_not_claim_progress():
    js = ADMIN_JS.read_text()
    assert 'if (!ran) {' in js
    assert "shutdown was not performed; the desktop is untouched" in js


def test_admin_js_still_parses():
    esprima = pytest.importorskip("esprima")
    esprima.parseScript(ADMIN_JS.read_text(), options={"tolerant": False})
