"""Regression tests for the Admin/Tools stabilization pass.

Each test below pins one concrete defect that made the Admin page require
knowledge of internal service state to use. They are grouped by the phase of
the failure they belong to: deployment drift, session/elevation handling,
Tools action visibility, and agent state accuracy.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from butters.actions.agent import AgentHub
from butters.assistant_config import load_assistant_settings
from butters.auth.manager import AuthenticationVerification
from butters.deployment import (MANIFEST_NAME, asset_version, checkout_digest,
                                describe, tree_digest)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService, ElevationRequired

ADMIN_JS = Path(__file__).parents[1] / "src/butters/web/static/assets/admin.js"


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


def _application(tmp_path):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
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


# =========================================================== agent state ====
#
# "If the desktop was shut down normally after the previous validation, Butters
# should not indefinitely claim that the interactive agent is still connected."


def _hub(tmp_path) -> AgentHub:
    return AgentHub(tmp_path / "absent-agents.toml")


def test_unconfigured_agent_reports_not_configured_not_merely_disconnected(tmp_path):
    status = _hub(tmp_path).status()
    assert status["agent_connected"] is False
    assert status["configured"] is False
    # "no agent has ever been configured here" and "the agent dropped off" are
    # different situations and the Tools page renders them differently.
    assert status["state"] == "not_configured"


def test_agent_without_heartbeat_is_never_reported_connected(tmp_path):
    """The 0 sentinel made an unauthenticated socket look live after a reboot.

    last_seen was initialized to 0 and compared against time.monotonic(), which
    on Linux is time since boot. Within the first 45 seconds of uptime that
    difference was itself below the staleness threshold, so a socket that had
    not yet sent an authenticated heartbeat reported agent_connected. The fix is
    a None sentinel, which is why this holds for any clock value.
    """

    hub = _hub(tmp_path)
    hub.config = {"agent_id": "desktop"}
    hub.ws = object()          # attached, but no heartbeat has arrived
    assert hub.last_seen is None

    status = hub.status()
    assert status["agent_connected"] is False
    assert status["state"] == "awaiting_heartbeat"
    assert status["last_heartbeat_age_seconds"] is None
    assert status["capabilities"]["gui_launch"] is False


def test_a_fresh_hub_uses_a_none_heartbeat_sentinel_not_zero(tmp_path):
    """Pins the sentinel itself, in both places it is assigned."""

    assert _hub(tmp_path).last_seen is None
    source = (Path(__file__).parents[1] / "src/butters/actions/agent.py").read_text()
    assert "self.last_seen = 0" not in source
    assert "self.last_seen = None  # Not READY until an authenticated heartbeat." in source


def test_stale_heartbeat_ages_out_and_is_named(tmp_path):
    hub = _hub(tmp_path)
    hub.config = {"agent_id": "desktop"}
    hub.ws = object()
    hub.session = {"state": "ACTIVE", "gui_launch": True, "interactive_session": True}
    hub.last_seen = time.monotonic() - (AgentHub.HEARTBEAT_STALE_SECONDS + 5)
    status = hub.status()
    assert status["agent_connected"] is False
    assert status["state"] == "heartbeat_stale"
    # The last observed session must not leak out once it is stale.
    assert status["session"] == {"state": "UNKNOWN"}
    assert status["interactive_session"] is False
    assert status["actions"] == []


def test_disconnect_drops_the_observed_session_snapshot(tmp_path):
    """A normally shut-down desktop must not leave its session standing."""

    hub = _hub(tmp_path)
    hub.config = {"agent_id": "desktop"}
    hub.ws = object()
    hub.actions = ["desktop.app.launch"]
    hub.session = {"state": "ACTIVE", "gui_launch": True, "interactive_session": True}
    hub.last_seen = time.monotonic()
    assert hub.status()["agent_connected"] is True

    hub._disconnect()

    status = hub.status()
    assert status["agent_connected"] is False
    assert status["state"] == "disconnected"
    assert status["session"] == {"state": "UNKNOWN"}
    assert status["last_heartbeat_age_seconds"] is None
    assert status["actions"] == []
    assert hub.session == {}


def test_gui_launch_is_withheld_while_the_heartbeat_ages(tmp_path):
    hub = _hub(tmp_path)
    hub.config = {"agent_id": "desktop"}
    hub.ws = object()
    hub.session = {"state": "ACTIVE", "gui_launch": True, "interactive_session": True}
    hub.last_seen = time.monotonic() - (AgentHub.HEARTBEAT_GUI_SECONDS + 1)
    status = hub.status()
    assert status["agent_connected"] is True
    assert status["state"] == "heartbeat_aging"
    assert status["capabilities"]["gui_launch"] is False


# ====================================================== deployment drift ====


def test_tree_digest_changes_when_a_deployed_file_is_hand_patched(tmp_path):
    root = tmp_path / "opt"
    (root / "src/butters").mkdir(parents=True)
    module = root / "src/butters/service.py"
    module.write_text("value = 1\n")
    before = tree_digest(root)
    module.write_text("value = 2\n")
    assert tree_digest(root) != before


def test_tree_digest_ignores_bytecode_and_runtime_state(tmp_path):
    root = tmp_path / "opt"
    (root / "src/butters/__pycache__").mkdir(parents=True)
    (root / "src/butters/service.py").write_text("value = 1\n")
    before = tree_digest(root)
    (root / "src/butters/__pycache__/service.pyc").write_bytes(b"\x00bytecode")
    (root / "src/butters/service.log").write_text("noise")
    assert tree_digest(root) == before


def test_renaming_a_file_changes_the_digest(tmp_path):
    root = tmp_path / "opt"
    (root / "src/butters").mkdir(parents=True)
    (root / "src/butters/one.py").write_text("value = 1\n")
    before = tree_digest(root)
    (root / "src/butters/one.py").rename(root / "src/butters/two.py")
    assert tree_digest(root) != before


def test_checkout_digest_matches_the_tree_the_installer_stages(tmp_path):
    """The shared agent protocol lives outside butters/src.

    install-beta1 stages butters-agent/src/butters_agent into src/butters_agent,
    so a checkout must hash with it mapped to that name. Otherwise every correct
    deployment reports itself as drifted and the signal becomes useless.
    """

    checkout = tmp_path / "repo"
    (checkout / "butters/src/butters").mkdir(parents=True)
    (checkout / "butters/src/butters/service.py").write_text("value = 1\n")
    (checkout / "butters-agent/src/butters_agent").mkdir(parents=True)
    (checkout / "butters-agent/src/butters_agent/protocol.py").write_text("SCHEMAS = {}\n")

    installed = tmp_path / "opt"
    (installed / "src/butters").mkdir(parents=True)
    (installed / "src/butters/service.py").write_text("value = 1\n")
    (installed / "src/butters_agent").mkdir(parents=True)
    (installed / "src/butters_agent/protocol.py").write_text("SCHEMAS = {}\n")

    assert checkout_digest(checkout / "butters") == tree_digest(installed)

    # And a divergence in the shared protocol is caught, not ignored.
    (checkout / "butters-agent/src/butters_agent/protocol.py").write_text("SCHEMAS = {'a': 1}\n")
    assert checkout_digest(checkout / "butters") != tree_digest(installed)


def test_installer_stages_the_shared_agent_protocol():
    """butters-web imports butters_agent.protocol and must not rely on a
    package somebody pip-installed into the venv by hand."""

    installer = (Path(__file__).parents[1] / "scripts/install-beta1").read_text()
    assert "butters-agent/src/butters_agent" in installer
    assert '"${staging_dir}/src/butters_agent/"' in installer
    # And the staged tree must be import-checked before it is published.
    assert "import butters.web.app" in installer


def test_describe_reports_hand_patching_after_an_install(tmp_path):
    """The exact failure this pass was opened to fix.

    /opt/butters had been installed days earlier and then patched file by file,
    so the deployed backend was several commits behind the deployed frontend and
    nothing reported it.
    """

    root = tmp_path / "opt"
    (root / "src/butters").mkdir(parents=True)
    (root / "src/butters/service.py").write_text("value = 1\n")
    (root / MANIFEST_NAME).write_text(
        json.dumps({"commit": "abc123", "branch": "main", "tree_digest": tree_digest(root)})
    )
    assert describe(root)["status"] == "installed"
    assert describe(root)["modified_since_install"] is False

    (root / "src/butters/service.py").write_text("value = 2\n")

    drifted = describe(root)
    assert drifted["status"] == "modified_since_install"
    assert drifted["modified_since_install"] is True
    assert drifted["commit"] == "abc123"


def test_describe_never_raises_on_a_missing_or_corrupt_manifest(tmp_path):
    root = tmp_path / "opt"
    (root / "src/butters").mkdir(parents=True)
    (root / "src/butters/service.py").write_text("value = 1\n")
    assert describe(root)["status"] == "unknown"
    (root / MANIFEST_NAME).write_text("{ this is not json")
    assert describe(root)["status"] == "unknown"
    assert describe(tmp_path / "absent")["status"] == "unknown"


def test_asset_version_is_stable_and_tracks_the_tree(tmp_path):
    root = tmp_path / "opt"
    (root / "src/butters/web/static/assets").mkdir(parents=True)
    asset = root / "src/butters/web/static/assets/admin.js"
    asset.write_text("console.log(1);")
    first = asset_version(root)
    assert first == asset_version(root)
    asset.write_text("console.log(2);")
    assert asset_version(root) != first


def test_installer_installs_and_restarts_every_daemon_running_the_tree():
    """A deployment that refreshes files without restarting is half applied."""

    installer = (Path(__file__).parents[1] / "scripts/install-beta1").read_text()
    assert "butters-agent-ingress.service" in installer
    assert "systemctl restart \"${ingress_unit_name}\"" in installer
    assert "butters-action-broker.service" in installer
    # And it must record what it installed.
    assert "from butters.deployment import MANIFEST_NAME, tree_digest" in installer


def test_asset_urls_are_versioned_so_no_browser_keeps_obsolete_javascript(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        headers = {"tailscale-user-login": "admin@example.com"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            page = await http.get("/admin", headers=headers)
            assert page.status_code == 200
            body = page.text
            assert "/assets/admin.js?v=" in body
            assert '/assets/admin.js"' not in body
            asset = await http.get("/assets/admin.js", headers=headers)
            assert asset.status_code == 200
            assert "no-cache" in asset.headers.get("cache-control", "")

    asyncio.run(scenario())


def test_overview_reports_the_deployment_so_drift_is_visible(tmp_path):
    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        headers = {"tailscale-user-login": "admin@example.com"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=headers)
            overview = await http.get("/api/admin/overview", headers=headers)
            assert overview.status_code == 200
            deployment = overview.json()["deployment"]
            assert set(deployment) >= {"status", "commit", "tree_digest", "modified_since_install"}

    asyncio.run(scenario())


# ================================================ session and elevation ====


def test_a_session_is_peer_bound_so_identity_cannot_change_under_it(tmp_path):
    """Context for the administrator-refresh fix, and a guard on its safety.

    Re-deriving the administrator flag is only sound because the session is
    already bound to the identity that created it, so the refresh can never
    read an identity belonging to somebody else. This pins that binding.
    """

    app, _service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            anonymous = await http.get("/api/session")
            assert anonymous.status_code == 200
            # Same cookie jar, a different caller identity.
            reused = await http.get(
                "/api/session", headers={"tailscale-user-login": "admin@example.com"}
            )
            assert reused.status_code == 403
            assert reused.json()["error"] == "session_identity_denied"

    asyncio.run(scenario())


def test_bound_session_administrator_flag_is_re_derived(tmp_path):
    """Unit-level pin for the same defect, independent of peer binding."""

    from butters.web.app import _refresh_administrator

    app, service = _application(tmp_path)
    session = service.sessions.create(peer_key="identity:admin@example.com", administrator=False)

    class Request:
        headers = {"tailscale-user-login": "admin@example.com"}

        class client:
            host = "127.0.0.1"

    class Policy:
        def is_administrator(self, headers, client_host):
            return headers.get("tailscale-user-login") == "admin@example.com"

    assert session.administrator is False
    _refresh_administrator(Request(), session, Policy())
    assert session.administrator is True

    # And it revokes, rather than only ever granting.
    class Anonymous(Request):
        headers: dict[str, str] = {}

    _refresh_administrator(Anonymous(), session, Policy())
    assert session.administrator is False


def test_expired_elevation_is_reported_as_reauthorize_not_dead_session(tmp_path):
    """The cryptic dead end.

    An expired elevation used to surface through the same code path as an
    invalid browser session, so the page told the user their session was gone
    when in fact one passkey ceremony would have resumed the action.
    """

    app, service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        headers = {"tailscale-user-login": "admin@example.com"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            session = (await http.get("/api/session", headers=headers)).json()
            mutation = {
                **headers,
                "origin": "http://testserver",
                "x-butters-csrf": session["csrf_token"],
            }
            # A registered compute action with no elevation held.
            response = await http.post(
                "/api/desktop/actions",
                headers=mutation,
                json={"action": "desktop.compile", "parameters": {"project": "anything"}},
            )
            assert response.status_code == 403
            payload = response.json()
            assert payload["error"] == "elevation_required"
            assert payload["reauthorize"] == "elevation"
            # Crucially, not a session code: the browser session is still good.
            assert payload["error"] not in {"invalid_session", "session_expired"}
            # And the read-only status endpoint still works, unelevated.
            assert (await http.get("/api/desktop/status", headers=headers)).status_code == 200

    asyncio.run(scenario())


def test_expired_browser_session_is_reported_distinctly(tmp_path):
    app, service = _application(tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        headers = {"tailscale-user-login": "admin@example.com"}
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            await http.get("/api/session", headers=headers)
            # Expire every session server-side, as an idle timeout would.
            service.sessions._sessions.clear()
            response = await http.get("/api/desktop/status", headers=headers)
            assert response.status_code == 401
            assert response.json()["error"] == "invalid_session"

    asyncio.run(scenario())


def test_elevation_required_is_a_permission_error_with_a_code():
    """Ordering matters: it must be caught before the generic handler."""

    error = ElevationRequired("needs elevation", action="desktop.compile")
    assert isinstance(error, PermissionError)
    assert error.code == "elevation_required"
    assert error.action == "desktop.compile"


def test_read_only_agent_actions_do_not_require_elevation(tmp_path):
    """Opening the Tools panel must not demand a passkey ceremony.

    desktop.app.list is how the panel discovers which applications exist. If it
    required elevation, an expired elevation would empty the panel -- which is
    how the Launch controls came to be missing.
    """

    _app, service = _application(tmp_path)
    spec = service.assistant.skills.get("desktop.app.list")
    from butters.skills.model import AuthenticationLevel

    assert spec.authentication is AuthenticationLevel.NONE
    launch = service.assistant.skills.get("desktop.app.launch")
    assert launch.authentication is AuthenticationLevel.ELEVATED


# ==================================================== Tools UI behaviour ====
#
# The Tools panel is plain browser JavaScript with no test runner in this
# repository, so these assert the structural properties that the reported
# failures came down to. They are deliberately about behaviour-carrying
# structure, not formatting.


def test_tools_sections_refresh_independently():
    source = ADMIN_JS.read_text()
    # One rejected subsection must not abort its siblings.
    assert "Promise.allSettled" in source
    for name in ("refreshDesktopStatus", "refreshDesktopCatalog",
                 "renderApplications", "renderVms"):
        assert f"async function {name}" in source


def test_application_rows_render_all_four_control_states():
    source = ADMIN_JS.read_text()
    for state in ("available", "unavailable", "not_configured", "requires_authorization"):
        assert state in source
    # A control that needs authorization stays clickable, because clicking it
    # is how the user re-elevates.
    assert 'state === "requires_authorization"' in source
    assert "function privilegedState" in source


def test_expired_elevation_does_not_hide_desktop_controls():
    source = ADMIN_JS.read_text()
    # privilegedState returns a visible state either way; it never returns a
    # value that removes the control.
    assert 'elevated ? ["available", availableReason]' in source
    assert '["requires_authorization"' in source


def test_missing_desktop_agent_key_is_handled_not_dereferenced():
    """A backend predating the agent work returned no desktop_agent key.

    The old code read agent.agent_connected straight off it, so the panel died
    with a TypeError instead of reporting the mismatch.
    """

    source = ADMIN_JS.read_text()
    assert "catalog ? catalog.desktop_agent : null" in source
    assert "if (!agent) return" in source


def test_session_expiry_is_announced_once_and_disables_stale_controls():
    source = ADMIN_JS.read_text()
    assert "function announceSessionExpired" in source
    assert "Reload and sign in" in source
    # Controls that would only fail on click are disabled up front.
    assert 'document.querySelectorAll(".admin-main button")' in source


def test_action_failures_restore_button_state_and_prevent_duplicates():
    source = ADMIN_JS.read_text()
    assert "if (desktopBusy || sessionDead) return;" in source
    assert "} finally {" in source
    assert "desktopBusy = false;" in source


def test_elevation_failure_message_names_the_action():
    source = ADMIN_JS.read_text()
    assert "requires renewed passkey authorization" in source
    # The old generic text must be gone.
    assert "Session expired. Reload this page, then authenticate" not in source


def test_desktop_state_axes_are_rendered_separately():
    """Not collapsed into a single ONLINE."""

    source = ADMIN_JS.read_text()
    assert "AXIS_LABELS" in source
    for axis in ("power", "network", "os", "session", "agent"):
        assert axis in source
    assert "AGENT_STATE_TEXT" in source
    for state in ("disconnected", "awaiting_heartbeat", "heartbeat_stale", "not_configured"):
        assert state in source


def test_transport_failure_is_not_reported_as_an_authorization_problem():
    source = ADMIN_JS.read_text()
    assert "network_unreachable" in source
    assert "It may be restarting" in source


def test_every_selector_admin_js_reaches_for_exists_in_the_page():
    """Rules out the commonest source of Admin console errors without a browser.

    querySelector returning null and then being dereferenced is how a renamed
    or removed element takes the whole page down at load, before any panel
    renders. This pins that every literal #id the script uses is either present
    in admin.html or created by the script itself.
    """

    import re

    html = (ADMIN_JS.parent.parent / "admin.html").read_text()
    js = ADMIN_JS.read_text()

    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'querySelector\("#([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'querySelectorAll\("#([A-Za-z0-9_-]+)', js))
    created = set(re.findall(r'\.id = "([^"]+)"', js))

    assert used, "no selectors found; the extraction pattern has drifted"
    assert not used - ids - created

    # Every navigable panel needs a title, or switching to it renders undefined.
    panels = set(re.findall(r'data-panel="([^"]+)"', html))
    titles_line = js[js.index("const titles"):js.index("\n", js.index("const titles"))]
    assert not panels - set(re.findall(r'(\w+):"', titles_line))


def test_admin_js_parses_as_a_script():
    """A syntax error in admin.js leaves the whole Admin page inert."""

    esprima = pytest.importorskip("esprima")
    esprima.parseScript(ADMIN_JS.read_text(), options={"tolerant": False})
