"""Desktop Admin parity on the current-main architecture.

Two things are asserted here. First, that every Desktop control the previous
production deployment offered is present in Admin -> Tools and that the NAS
panel sits beside it rather than replacing it. Second, that the state model no
longer contradicts itself: CURRENT OBSERVED STATE and LAST OPERATION are
separate, and an old wake record cannot relabel an authoritative agent report.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import httpx
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ADMIN_HTML = (STATIC / "admin.html").read_text()
ADMIN_JS = (STATIC / "assets/admin.js").read_text()


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


# ------------------------------ UI parity -----------------------------------


def test_admin_tools_contains_every_production_desktop_control() -> None:
    for element in (
        'id="desktop-status"',
        'id="desktop-summary"',
        'id="desktop-refresh"',
        'id="desktop-ssh-test"',
        'id="desktop-wake"',
        'id="desktop-agent-status"',
        'id="desktop-apps"',
        'id="desktop-vms"',
        'id="desktop-compute-note"',
        'id="desktop-shutdown"',
        'id="desktop-shutdown-confirm"',
        'id="desktop-result"',
    ):
        assert element in ADMIN_HTML, element


def test_admin_tools_exposes_the_named_interactive_applications() -> None:
    """Git Bash, Parsec, VS Code and File Explorer reach the desktop by name.

    Butters holds no executable for any of them. The catalog is whatever the
    connected agent reports from its own allowlist, so the assertion is that
    the UI renders that catalog and launches by symbolic name.
    """

    assert 'id="desktop-apps"' in ADMIN_HTML
    assert "renderDesktopApps" in ADMIN_JS
    assert "/api/admin/tools/desktop/launch-app" in ADMIN_JS
    assert "app.display_name||app.app" in ADMIN_JS


def test_nas_panel_coexists_with_the_desktop_panel() -> None:
    assert 'id="desktop-card"' in ADMIN_HTML
    assert 'id="nas-card"' in ADMIN_HTML
    assert ADMIN_HTML.index('id="desktop-card"') < ADMIN_HTML.index('id="nas-card"')
    for element in (
        'id="nas-status"',
        'id="nas-refresh"',
        'id="nas-wake"',
        'id="nas-shutdown"',
        'id="nas-last-operation"',
        'id="nas-action-status"',
    ):
        assert element in ADMIN_HTML, element


def test_admin_never_posts_a_caller_named_desktop_action() -> None:
    """The historical generic execution path must not come back."""

    assert "/api/desktop/actions" not in ADMIN_JS
    assert "/api/desktop/catalog" not in ADMIN_JS
    source = (
        Path(__file__).parents[1] / "src/butters/web/app.py"
    ).read_text()
    assert '"/api/desktop/actions"' not in source
    assert '"/api/desktop/catalog"' not in source
    assert "desktop.run_registered" not in source


def test_last_operation_and_observed_state_render_separately() -> None:
    assert 'id="desktop-last-operation"' in ADMIN_HTML
    assert 'id="desktop-status"' in ADMIN_HTML
    # The agent line is written only from `observed`, never from history: take
    # exactly the assignment that produces it and check what it reads.
    start = ADMIN_JS.index('document.querySelector("#desktop-agent-status").textContent')
    assignment = ADMIN_JS[start : ADMIN_JS.index(";", start)]
    assert "observed.agent_connected" in assignment
    assert "observed.windows_session" in assignment
    assert "last_operation" not in assignment
    # And the history line is rendered by its own helper into its own node.
    assert 'renderLastOperation(document.querySelector("#desktop-last-operation")' in ADMIN_JS


def test_vm_empty_state_explains_itself() -> None:
    assert "VM capability has not been checked." in ADMIN_HTML
    service_source = (
        Path(__file__).parents[1] / "src/butters/web/service.py"
    ).read_text()
    assert "Virtual machines are not monitored" in service_source
    assert "This is not a statement that the desktop has no VMs." in service_source


# --------------------------- state-model behaviour ---------------------------


def _application(tmp_path):
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        broker=replace(base.broker, enabled=True),
        desktop=replace(base.desktop, wake_enabled=True, shutdown_enabled=True),
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
    return create_app(settings, vocabulary, service, stt_engine_factory=Engine), service


class _ConnectedAgent:
    """An authoritative agent report: connected, active session, fresh beat."""

    configured = True

    def status(self):
        return {
            "configured": True,
            "state": "connected",
            "agent_connected": True,
            "interactive_session": "present",
            "reason": None,
            "connected_since": time.time() - 60,
            "last_authenticated_activity_age_seconds": 2.0,
            "version": "1.0.0",
            "protocol": 1,
            "reconnect_count": 1,
        }


async def _stale_wake_cannot_contradict_a_connected_agent(tmp_path) -> None:
    app, service = _application(tmp_path)
    service.desktop_agent = _ConnectedAgent()
    # A wake workflow from ten minutes ago, exactly the situation that used to
    # produce "agent OFFLINE" beside "Desktop Agent CONNECTED".
    service._last_operations["desktop"] = {
        "subject": "desktop",
        "operation": "wake_desktop",
        "outcome": "queued",
        "detail": "",
        "at": time.time() - 600,
    }
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            await http.get("/api/session", headers=headers)
            state = (
                await http.get("/api/admin/tools/desktop", headers=headers)
            ).json()
            observed = state["observed"]
            assert observed["agent"] == "connected"
            assert observed["agent_connected"] is True
            assert observed["windows_session"] == "present"
            # The headline is derived from the observation, not from history.
            assert "agent connected" in state["summary"]
            assert "Windows session active" in state["summary"]
            assert "offline" not in state["summary"].lower()
            # The stale record is still reported, but only as last operation.
            assert state["last_operation"]["operation"] == "wake_desktop"
            assert state["last_operation"]["age_seconds"] > 500
            assert "agent" not in state["last_operation"]
    finally:
        await app.state.shutdown_workers()
        del service


async def _unknown_session_is_not_reported_as_absent(tmp_path) -> None:
    app, service = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"tailscale-user-login": "admin@example.com"}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            await http.get("/api/session", headers=headers)
            state = (
                await http.get("/api/admin/tools/desktop", headers=headers)
            ).json()
            observed = state["observed"]
            # With no agent configured nothing is claimed either way.
            assert observed["agent"] in {"not_configured", "disconnected"}
            assert observed["windows_session"] == "unknown"
            assert observed["power_network"] in {"yes", "no", "unknown"}
            assert state["apps"]["observed"] is False
            assert state["vm"]["observed"] is False
            assert state["last_operation"] is None
    finally:
        await app.state.shutdown_workers()
        del service


async def _desktop_controls_require_administrator(tmp_path) -> None:
    app, service = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            session = (await http.get("/api/session")).json()
            mutation = {
                "origin": "http://testserver",
                "x-butters-csrf": session["csrf_token"],
            }
            for path, payload in (
                ("/api/admin/tools/desktop/wake", {}),
                ("/api/admin/tools/desktop/shutdown", {}),
                ("/api/admin/tools/desktop/ssh-test", {}),
                ("/api/admin/tools/desktop/launch-app", {"app": "parsec"}),
            ):
                response = await http.post(path, headers=mutation, json=payload)
                assert response.status_code in {401, 403}, path
            denied = await http.get("/api/admin/tools/desktop")
            assert denied.status_code in {401, 403}
    finally:
        await app.state.shutdown_workers()
        del service


async def _launch_rejects_anything_but_a_symbolic_name(tmp_path) -> None:
    app, service = _application(tmp_path)
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
            for payload in (
                {"app": "C:/Windows/System32/cmd.exe"},
                {"app": "parsec; shutdown"},
                {"app": "../../escape"},
                {"app": ""},
                {"app": 12},
                {"app": "parsec", "argv": ["x"]},
                {"command": "parsec"},
                {"skill": "shutdown_desktop"},
                {},
            ):
                response = await http.post(
                    "/api/admin/tools/desktop/launch-app",
                    headers=mutation,
                    json=payload,
                )
                assert response.status_code == 400, payload
    finally:
        await app.state.shutdown_workers()
        del service


def test_stale_wake_cannot_contradict_a_connected_agent(tmp_path) -> None:
    asyncio.run(_stale_wake_cannot_contradict_a_connected_agent(tmp_path))


def test_unknown_session_is_not_reported_as_absent(tmp_path) -> None:
    asyncio.run(_unknown_session_is_not_reported_as_absent(tmp_path))


def test_desktop_controls_require_administrator(tmp_path) -> None:
    asyncio.run(_desktop_controls_require_administrator(tmp_path))


def test_launch_rejects_anything_but_a_symbolic_name(tmp_path) -> None:
    asyncio.run(_launch_rejects_anything_but_a_symbolic_name(tmp_path))
