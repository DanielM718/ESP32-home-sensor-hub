"""The Admin surface for provider accounting: endpoints, FRESH, and the page."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from pathlib import Path

import httpx
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService, _accounting_windows

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (STATIC / "assets/admin.js").read_text(encoding="utf-8")
ORIGIN = "https://butters.example.ts.net"
ADMIN = "admin@example.com"
SECRET = "sk-admin-" + "a" * 40


def _app(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=False,
            admin_identities=(ADMIN,),
            allowed_origins=(ORIGIN,),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    service = BetaAssistantService(settings, DomainVocabulary((), ()), state_dir=tmp_path)

    class Engine:
        initialization_seconds = 0.0

        def close(self):
            return None

    return create_app(settings, DomainVocabulary((), ()), service, stt_engine_factory=Engine), service


def _run(app, scenario):
    async def main():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4242))
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, timeout=20) as http:
            await scenario(http)
        await app.state.shutdown_workers()

    asyncio.run(main())


def _headers(identity: str | None = ADMIN) -> dict[str, str]:
    headers = {"Origin": ORIGIN}
    if identity:
        headers["Tailscale-User-Login"] = identity
    return headers


# ============================== endpoints ==================================


def test_the_usage_admin_surface_is_administrator_only(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        await http.get("/api/session", headers=_headers(None))
        assert (
            await http.get("/api/admin/integrations/openai-usage", headers=_headers(None))
        ).status_code in (401, 403)

    _run(app, scenario)


def test_an_unconfigured_provider_reports_a_state_not_an_error(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        await http.get("/api/session", headers=_headers())
        response = await http.get("/api/admin/integrations/openai-usage", headers=_headers())
        assert response.status_code == 200
        body = response.json()
        assert body["configured"] is False
        assert body["freshness"] == "not_configured"
        assert body["scope_kind"] == "organization"

    _run(app, scenario)


def test_storing_the_admin_key_requires_a_fresh_grant(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        # Two attempts: credential mutation shares the expensive rate limiter,
        # whose burst is small on purpose, and a 429 would prove nothing here.
        for body in (
            {"admin_api_key": SECRET, "confirm": True},
            {"admin_api_key": SECRET, "confirm": True, "fresh_grant": "not-a-real-grant"},
        ):
            refused = await http.post(
                "/api/admin/integrations/openai-usage/key", headers=mutate, json=body
            )
            assert refused.status_code in (400, 401, 403), body
            assert refused.json()["error"] in {
                "fresh_required", "fresh_grant_denied", "fresh_binding_denied",
            }
        # Nothing was stored by any of those attempts.
        state = (await http.get("/api/admin/integrations/openai-usage", headers=_headers())).json()
        assert state["configured"] is False

    _run(app, scenario)


def test_removing_the_admin_key_requires_a_fresh_grant(tmp_path, monkeypatch) -> None:
    app, service = _app(tmp_path, monkeypatch)
    service.usage_admin_credentials.store(SECRET)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        refused = await http.request(
            "DELETE",
            "/api/admin/integrations/openai-usage/key",
            headers=mutate,
            json={"confirm": True},
        )
        assert refused.status_code in (400, 401, 403)
        assert service.usage_admin_credentials.configured() is True

    _run(app, scenario)


def test_an_unsupported_field_is_refused(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        refused = await http.post(
            "/api/admin/integrations/openai-usage/key",
            headers=mutate,
            json={"admin_api_key": SECRET, "confirm": True, "endpoint": "/v1/organization/users"},
        )
        assert refused.status_code == 400

    _run(app, scenario)


def test_no_endpoint_can_return_either_credential(tmp_path, monkeypatch) -> None:
    """The inference key and the Admin key are both unreachable by read."""

    app, service = _app(tmp_path, monkeypatch)
    service.usage_admin_credentials.store(SECRET)
    service.ai.credentials.store("sk-inference-" + "b" * 30, validation=None)

    async def scenario(http):
        await http.get("/api/session", headers=_headers())
        for path in (
            "/api/admin/integrations/openai-usage",
            "/api/admin/integrations/openai",
            "/api/admin/ai/settings",
            "/api/admin/usage",
            "/api/admin/security",
            "/api/admin/system",
        ):
            body = (await http.get(path, headers=_headers())).content
            assert SECRET.encode() not in body, path
            assert b"sk-inference" not in body, path
            assert b"Bearer" not in body, path

    _run(app, scenario)


def test_the_usage_report_carries_provider_state_alongside_local(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        await http.get("/api/session", headers=_headers())
        body = (await http.get("/api/admin/usage", headers=_headers())).json()
        assert "summary" in body and "provider" in body
        # Separate keys: the provider figure is never merged into the local one.
        assert "reported_cost_usd" not in body["summary"]
        assert body["provider"]["configured"] is False

    _run(app, scenario)


def test_a_project_scope_must_be_an_exact_identifier(tmp_path, monkeypatch) -> None:
    app, _service = _app(tmp_path, monkeypatch)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        refused = await http.post(
            "/api/admin/integrations/openai-usage/project",
            headers=mutate,
            json={"project_id": "butters-prod"},
        )
        assert refused.status_code == 400
        assert refused.json()["error"] == "invalid_project_id"
        accepted = await http.post(
            "/api/admin/integrations/openai-usage/project",
            headers=mutate,
            json={"project_id": "proj_abcdefgh1234"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["scope"] == "proj_abcdefgh1234"

    _run(app, scenario)


def test_a_provider_outage_never_breaks_chat_or_the_usage_page(tmp_path, monkeypatch) -> None:
    """The whole point of keeping the two ledgers apart."""

    app, service = _app(tmp_path, monkeypatch)
    service.usage_admin_credentials.store(SECRET)

    def explode(*args, **kwargs):
        raise OSError("billing API is down")

    service.provider_accounting.client._opener = explode

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        synced = await http.post(
            "/api/admin/integrations/openai-usage/sync", headers=mutate, json={}
        )
        assert synced.status_code == 200
        assert synced.json()["failure_code"] == "unavailable"
        # And everything else still answers.
        assert (await http.get("/api/admin/usage", headers=_headers())).status_code == 200
        assert (await http.get("/api/admin/ai/settings", headers=_headers())).status_code == 200
        assert (await http.get("/healthz")).status_code == 200
        assert (await http.get("/readyz")).status_code == 200

    _run(app, scenario)


def test_the_windows_are_aligned_to_whole_utc_days() -> None:
    """Provider buckets are whole UTC days; a partial window would invent a gap."""

    for start, end in _accounting_windows().values():
        assert start % 86400 == 0
        assert end % 86400 == 0
        assert end > start


# ================================= the page ================================


def test_the_credential_card_exists_and_never_shows_a_key() -> None:
    for control in ("openai-usage-card", "usage-admin-summary", "usage-admin-state",
                    "usage-admin-project", "usage-admin-sync", "usage-admin-test",
                    "usage-admin-set", "usage-admin-remove", "usage-admin-form",
                    "usage-admin-key", "usage-admin-remove-confirm", "usage-admin-status"):
        assert f'id="{control}"' in ADMIN_HTML, control
    card = ADMIN_HTML[ADMIN_HTML.index('id="openai-usage-card"'):]
    card = card[: card.index("</section>")]
    assert 'id="usage-admin-key" type="password"' in card
    assert "never stored in this browser" in card
    # The powerful nature of an Admin key is stated, not glossed.
    assert "organization Admin API key" in card
    assert "no usage-only Admin scope" in card


def test_both_mutations_use_the_distinct_fresh_purpose() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ====================== Provider-reported accounting"):]
    assert 'authenticatePurpose("openai_usage_admin_credential", "set")' in section
    assert 'authenticatePurpose("openai_usage_admin_credential", "remove")' in section
    # Never the inference credential's purpose.
    assert '"openai_credential"' not in section


def test_the_page_never_calls_openai_directly() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ====================== Provider-reported accounting"):]
    assert "api.openai.com" not in section
    assert "openai.com" not in section
    for call in re.findall(r'api\("([^"]+)"', section):
        assert call.startswith("/api/admin/"), call


def test_the_spend_section_separates_provider_from_local() -> None:
    assert 'id="usage-reconciliation"' in ADMIN_HTML
    assert 'id="usage-provider-lines"' in ADMIN_HTML
    panel = ADMIN_HTML[ADMIN_HTML.index('id="panel-usage"'):]
    # The local figure is labelled as a reservation, not a charge.
    assert "Butters local accounting" in panel
    assert "deliberate ceiling, not a charge" in panel
    # And Butters requests are distinguished from provider operations.
    assert "counts conversation turns" in panel


def test_the_difference_is_never_called_a_saving() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ====================== Provider-reported accounting"):]
    assert "not a saving" in section
    assert "unreconciled estimate" in section
    # Every mention of the word is a denial of it, in prose or on screen.
    for occurrence in re.finditer(r"sav(ing|ed)", section, re.IGNORECASE):
        context = section[max(0, occurrence.start() - 30) : occurrence.end()]
        assert re.search(r"\b(not|never)\b", context, re.IGNORECASE), context


def test_a_difference_is_only_shown_for_a_comparable_reading() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function renderReconciliation"):]
    block = block[: block.index("\n}\n")]
    assert "comparable" in block
    assert 'provider.freshness !== "never_synced"' in block
    assert 'typeof providerValue === "number"' in block


def test_an_unscoped_reading_is_labelled_organization_wide() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function renderReconciliation"):]
    block = block[: block.index("\n}\n")]
    assert "organization-wide" in block
    assert "not Butters-only" in block


def test_freshness_and_window_are_shown() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ====================== Provider-reported accounting"):]
    assert "Last sync" in section
    assert "reporting_window" in section
    assert "stale" in section
    # Never claimed as an exact current figure.
    assert "current exact spend" not in section
    assert "Provider reported" in ADMIN_HTML or "OpenAI reported" in section


def test_the_raw_provider_payload_is_not_rendered() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ====================== Provider-reported accounting"):]
    assert "pretty(provider" not in section
    assert "JSON.stringify(provider" not in section
