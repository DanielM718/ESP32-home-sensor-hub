"""Production-mode authorization, origin policy, and privileged-route coverage."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from beta1_harness import ADMIN_IDENTITY, PRODUCTION_ORIGIN, admin_headers, build_app, client
from butters.assistant_config import WebSettings
from butters.web.security import AuthPolicy, SecurityError


PRODUCTION: dict[str, object] = {
    "development_mode": False,
    "allowed_origins": (PRODUCTION_ORIGIN,),
}

# Every privileged route, so a new one cannot quietly skip authorization.
ADMIN_GET_ROUTES = (
    "/api/admin/overview",
    "/api/admin/traces",
    "/api/admin/sessions",
    "/api/admin/models",
    "/api/admin/skills",
    "/api/admin/tools",
    "/api/admin/usage",
    "/api/admin/security",
    "/api/admin/voice/presets",
    "/api/admin/system",
    "/api/admin/logs",
    "/api/admin/codex/jobs",
    "/api/admin/codex/jobs/abcdefghijklmnop",
)
ADMIN_POST_ROUTES = (
    "/api/admin/routing/test",
    "/api/admin/stt/test",
    "/api/admin/skills/toggle",
    "/api/admin/skills/test",
    "/api/admin/voice/presets",
    "/api/admin/voice/preview",
    "/api/admin/codex/jobs",
    "/api/admin/codex/jobs/abcdefghijklmnop/run",
    "/api/admin/codex/jobs/abcdefghijklmnop/decision",
)


def test_admin_page_returns_403_not_500_without_identity(tmp_path: Path) -> None:
    """M-2: an unauthorized /admin must fail closed with a bounded 403 body."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                denied = await http.get("/admin")
                assert denied.status_code == 403
                assert denied.json()["error"] == "admin_identity_missing"

                unlisted = await http.get("/admin", headers={"tailscale-user-login": "intruder@example.com"})
                assert unlisted.status_code == 403
                assert unlisted.json()["error"] == "admin_identity_denied"

                allowed = await http.get("/admin", headers=admin_headers())
                assert allowed.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ADMIN_GET_ROUTES)
def test_privileged_get_routes_reject_an_unlisted_identity(tmp_path: Path, path: str) -> None:
    """M-2/M-8: authorization is enforced by the server on every admin route."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                anonymous = await http.get(path)
                assert anonymous.status_code == 403, path
                unlisted = await http.get(path, headers={"tailscale-user-login": "intruder@example.com"})
                assert unlisted.status_code == 403, path
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ADMIN_POST_ROUTES)
def test_privileged_post_routes_reject_an_unlisted_identity(tmp_path: Path, path: str) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                response = await http.post(
                    path,
                    headers={
                        "tailscale-user-login": "intruder@example.com",
                        "origin": PRODUCTION_ORIGIN,
                        "content-type": "application/json",
                    },
                    content=b"{}",
                )
                assert response.status_code == 403, path
                assert response.json()["error"] == "admin_identity_denied", path
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_production_session_cookie_is_secure_httponly_and_samesite_strict(tmp_path: Path) -> None:
    """Production cookies must not be replayable cross-site or over plaintext."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                response = await http.get("/api/session", headers={"sec-fetch-site": "same-origin"})
                assert response.status_code == 200
                cookie = response.headers["set-cookie"].lower()
                assert "httponly" in cookie
                assert "secure" in cookie
                assert "samesite=strict" in cookie
                assert "path=/" in cookie
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_production_mutation_requires_an_allow_listed_https_origin(tmp_path: Path) -> None:
    """L-4: the production trust anchor is the server-known origin, not Host."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                session = await http.get("/api/session", headers={"sec-fetch-site": "same-origin"})
                token = session.json()["csrf_token"]

                forged = await http.post(
                    "/api/chat",
                    headers={
                        "origin": "https://attacker.example",
                        "host": "attacker.example",
                        "x-butters-csrf": token,
                    },
                    json={"text": "what is the humidity in box three"},
                )
                assert forged.status_code == 403
                assert forged.json()["error"] == "origin_denied"

                plaintext = await http.post(
                    "/api/chat",
                    headers={"origin": "http://butters.example-tailnet.ts.net", "x-butters-csrf": token},
                    json={"text": "what is the humidity in box three"},
                )
                assert plaintext.status_code == 403

                accepted = await http.post(
                    "/api/chat",
                    headers={"origin": PRODUCTION_ORIGIN, "x-butters-csrf": token},
                    json={"text": "what is the humidity in box three"},
                )
                assert accepted.status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_missing_production_origin_fails_closed_and_is_reported(tmp_path: Path) -> None:
    """L-4: an unconfigured production origin must fail loudly, not silently."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, development_mode=False, allowed_origins=())
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                ready = await http.get("/readyz")
                assert ready.status_code == 503
                assert ready.json()["checks"]["production_origin"] == "unconfigured"

                session = await http.get("/api/session", headers={"sec-fetch-site": "same-origin"})
                assert session.status_code == 503
                assert session.json()["error"] == "origin_not_configured"

                # Liveness is unaffected so the operator can still reach the host.
                assert (await http.get("/healthz")).status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_production_session_allocation_requires_a_browser_context(tmp_path: Path) -> None:
    """H-1/L-4: a scripted tailnet caller cannot silently mint sessions."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                scripted = await http.get("/api/session")
                assert scripted.status_code == 403
                assert scripted.json()["error"] == "browser_context_required"

                http.cookies.clear()
                browser = await http.get("/api/session", headers={"sec-fetch-site": "same-origin"})
                assert browser.status_code == 200

                http.cookies.clear()
                by_origin = await http.get("/api/session", headers={"origin": PRODUCTION_ORIGIN})
                assert by_origin.status_code == 200

                http.cookies.clear()
                foreign = await http.get("/api/session", headers={"origin": "https://attacker.example"})
                assert foreign.status_code == 403
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_admin_html_is_not_served_from_the_public_asset_mount(tmp_path: Path) -> None:
    """L-1: the admin document is authorization-gated, not a public asset."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path, **PRODUCTION)
        try:
            async with client(app, base_url=PRODUCTION_ORIGIN) as http:
                assert (await http.get("/assets/admin.html")).status_code == 404
                assert (await http.get("/assets/index.html")).status_code == 404
                # Shared front-end code stays reachable so the pages still work.
                assert (await http.get("/assets/styles.css")).status_code == 200
                assert (await http.get("/assets/app.js")).status_code == 200
                assert (await http.get("/assets/admin.js")).status_code == 200
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


def test_no_test_only_peer_identity_is_trusted_in_production() -> None:
    """L-2: only real loopback peers may carry proxy-supplied identity."""

    policy = AuthPolicy(
        WebSettings(admin_identities=(ADMIN_IDENTITY,)),
        environment={},
    )
    assert policy.admin_identity({"tailscale-user-login": ADMIN_IDENTITY}, "127.0.0.1") == ADMIN_IDENTITY
    for peer in ("testclient", "100.64.0.10", "192.168.1.5", None):
        with pytest.raises(SecurityError) as denied:
            policy.admin_identity({"tailscale-user-login": ADMIN_IDENTITY}, peer)
        assert denied.value.code == "untrusted_proxy_peer"


def test_development_admin_bypass_is_inert_without_development_mode() -> None:
    """L-2: the convenience bypass cannot be reached from a production default."""

    production = AuthPolicy(
        WebSettings(admin_identities=()),
        environment={"BUTTERS_DEV_ADMIN": "1"},
    )
    with pytest.raises(SecurityError):
        production.admin_identity({}, "127.0.0.1")

    development = AuthPolicy(
        WebSettings(development_mode=True),
        environment={"BUTTERS_DEV_ADMIN": "1"},
    )
    assert development.admin_identity({}, "127.0.0.1") == "development-local-admin"


def test_peer_key_distinguishes_tailnet_callers_behind_one_proxy() -> None:
    """H-1: fairness keys on the proxied identity, not the shared socket peer."""

    policy = AuthPolicy(WebSettings(admin_identities=(ADMIN_IDENTITY,)), environment={})
    first = policy.peer_key({"tailscale-user-login": "one@example.com"}, "127.0.0.1")
    second = policy.peer_key({"tailscale-user-login": "TWO@example.com"}, "127.0.0.1")
    anonymous = policy.peer_key({}, "127.0.0.1")
    assert first != second
    assert first == policy.peer_key({"tailscale-user-login": "ONE@example.com"}, "127.0.0.1")
    assert anonymous.startswith("peer:")


def test_environment_admin_identities_are_read_from_the_injected_environment() -> None:
    policy = AuthPolicy(
        WebSettings(),
        environment={"BUTTERS_ADMIN_IDENTITIES": "one@example.com, two@example.com"},
    )
    assert policy.admin_identity({"tailscale-user-login": "TWO@example.com"}, "::1") == "TWO@example.com"
    with pytest.raises(SecurityError):
        policy.admin_identity({"tailscale-user-login": "three@example.com"}, "::1")
