"""Provider-reported accounting: isolated, observational, and never exact.

Butters keeps two accounting concepts. The local ledger decides, before a
paid call, whether it is affordable, and over-estimates where a billable
dimension is not observable. Provider reporting says what OpenAI actually
billed, after the fact. These tests hold the line between them, and hold the
organization Admin key away from everything that is not an accounting read.
"""

from __future__ import annotations

import ast
import io
import json
import sqlite3
import tokenize
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from butters.cloud.provider_accounting import (
    ALLOWED_PATHS,
    API_HOST,
    AUDIO_SPEECHES,
    COMPLETIONS,
    COSTS,
    MAX_RESPONSE_BYTES,
    AccountingError,
    OpenAIAccountingClient,
    ProviderAccountingService,
    ProviderAccountingStore,
    _NoRedirects,
)
from butters.cloud.usage_admin_credential import (
    CREDENTIAL_CLASS,
    UsageAdminCredentialError,
    UsageAdminCredentialStore,
    normalize_candidate,
)

SECRET = "sk-admin-" + "a" * 40
SOURCE = Path(__file__).parents[1] / "src/butters"


def executable(name: str) -> str:
    """A module with comments and docstrings removed.

    These assertions are about what the code *does*. Prose that explains why
    a thing is forbidden would otherwise trip the very check it documents.
    """

    body = (SOURCE / name).read_text()
    without_comments = tokenize.untokenize(
        token
        for token in tokenize.generate_tokens(io.StringIO(body).readline)
        if token.type != tokenize.COMMENT
    )
    tree = ast.parse(without_comments)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (
            node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body.pop(0)
    return ast.unparse(tree)


def imported_modules(name: str) -> set[str]:
    tree = ast.parse((SOURCE / name).read_text())
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules

DAY = 86400
BASE = 1_789_000_000 // DAY * DAY


# ------------------------------- fixtures ---------------------------------


def _bucket(start: int, results: list[dict]) -> dict:
    return {"object": "bucket", "start_time": start, "end_time": start + DAY, "results": results}


def costs_page(*, buckets: int = 2, has_more: bool = False, next_page: str | None = None) -> dict:
    return {
        "object": "page",
        "data": [
            _bucket(
                BASE + index * DAY,
                [
                    {
                        "object": "organization.costs.result",
                        "amount": {"value": 0.01, "currency": "usd"},
                        "line_item": "gpt-4o-mini-tts, audio output tokens",
                        "project_id": "proj_butters",
                    }
                ],
            )
            for index in range(buckets)
        ],
        "has_more": has_more,
        "next_page": next_page,
    }


def speeches_page() -> dict:
    return {
        "object": "page",
        "data": [
            _bucket(
                BASE,
                [
                    {
                        "object": "organization.usage.audio_speeches.result",
                        "characters": 1200,
                        "num_model_requests": 25,
                        "model": "gpt-4o-mini-tts",
                    }
                ],
            )
        ],
        "has_more": False,
        "next_page": None,
    }


def completions_page() -> dict:
    return {
        "object": "page",
        "data": [
            _bucket(
                BASE,
                [
                    {
                        "object": "organization.usage.completions.result",
                        "input_tokens": 386,
                        "output_tokens": 69,
                        "num_model_requests": 2,
                        "model": "gpt-5.6-terra",
                    }
                ],
            )
        ],
        "has_more": False,
        "next_page": None,
    }


class Recorder:
    """A fake opener that records every request and returns canned pages."""

    def __init__(self, pages: dict[str, object] | None = None, error: Exception | None = None) -> None:
        self.requests: list[urllib.request.Request] = []
        self.error = error
        self.pages = pages or {
            COSTS: costs_page(),
            COMPLETIONS: completions_page(),
            AUDIO_SPEECHES: speeches_page(),
        }

    def __call__(self, request: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        path = urllib.request.urlparse(request.full_url).path
        return json.dumps(self.pages[path]).encode("utf-8")


@pytest.fixture
def configured(tmp_path):
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    recorder = Recorder()
    service = ProviderAccountingService(
        credentials,
        store,
        OpenAIAccountingClient(credentials, opener=recorder),
        clock=lambda: float(BASE + 2 * DAY),
    )
    return service, store, credentials, recorder


# ============================== 1. isolation ===============================


def test_the_admin_key_is_a_different_class_in_a_different_file(tmp_path) -> None:
    inference = tmp_path / "credentials/openai-api-key"
    admin = UsageAdminCredentialStore(tmp_path)
    admin.store(SECRET)
    assert admin.key_path.name == "openai-usage-admin-key"
    assert admin.key_path != inference
    assert not inference.exists()
    assert CREDENTIAL_CLASS == "openai_usage_admin_key"


def test_the_stored_admin_key_is_private_on_disk(tmp_path) -> None:
    store = UsageAdminCredentialStore(tmp_path)
    store.store(SECRET)
    assert oct(store.key_path.stat().st_mode & 0o777) == "0o600"
    assert oct(store.directory.stat().st_mode & 0o777) == "0o700"


def test_no_projection_of_the_admin_credential_carries_the_secret(tmp_path) -> None:
    store = UsageAdminCredentialStore(tmp_path)
    store.store(SECRET)
    serialized = json.dumps(store.state().as_dict())
    assert SECRET not in serialized
    assert "sk-" not in serialized
    # Not even a fingerprint: there is one Admin key, so it identifies rather
    # than disambiguates.
    assert "fingerprint" not in serialized


def test_the_admin_key_has_no_environment_fallback(tmp_path, monkeypatch) -> None:
    """An Admin key must be put here deliberately, never inherited."""

    monkeypatch.setenv("OPENAI_API_KEY", "sk-inference-key-from-the-unit")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-from-the-unit")
    store = UsageAdminCredentialStore(tmp_path)
    assert store.secret() is None
    assert store.configured() is False


def test_the_accounting_modules_never_reach_inference(tmp_path) -> None:
    """Grep-level, because the guarantee is that the import does not exist."""

    for name in ("cloud/provider_accounting.py", "cloud/usage_admin_credential.py"):
        modules = imported_modules(name)
        # Nothing from the inference stack, and nothing from the credential
        # module that serves it.
        assert not [item for item in modules if item.startswith("butters.ai")], name
        assert not [item for item in modules if item.startswith("butters.tts")], name
        assert not [item for item in modules if item.startswith("butters.stt")], name
        assert not [item for item in modules if item.startswith("butters.llm")], name


def test_inference_construction_never_receives_the_admin_store() -> None:
    runtime = (SOURCE / "ai/runtime.py").read_text()
    assert "usage_admin" not in runtime
    assert "UsageAdminCredentialStore" not in runtime
    speech = (SOURCE / "web/speech.py").read_text()
    assert "usage_admin" not in speech
    # The admin store is constructed once and handed only to the accounting
    # service and its client.
    service = executable("web/service.py")
    construction = service[service.index("self.usage_admin_credentials ="):]
    construction = construction[: construction.index("self.provider_accounting =") + 400]
    assert "ProviderAccountingService" in construction
    assert "OpenAIAccountingClient" in construction
    # It never reaches a provider bundle or a speech path.
    for line in service.splitlines():
        if "usage_admin_credentials" not in line:
            continue
        assert "ProviderBundle" not in line
        assert "reasoner" not in line.lower()
        assert "speech" not in line.lower()


@pytest.mark.parametrize("candidate", ["", "   ", "short", 42, None, "has space inside",
                                       "nonascii-é" + "x" * 30])
def test_a_candidate_that_is_not_a_bearer_token_is_refused(candidate) -> None:
    with pytest.raises(UsageAdminCredentialError) as refused:
        normalize_candidate(candidate)
    assert refused.value.code == "invalid_credential"
    # The rejection never quotes what was pasted.
    assert str(candidate)[:12] not in refused.value.message or len(str(candidate)) < 4


# ============================ 2. client limits =============================


def test_only_three_endpoints_exist(configured) -> None:
    assert ALLOWED_PATHS == {COSTS, COMPLETIONS, AUDIO_SPEECHES}
    client = configured[0].client
    # No generic request surface.
    assert not hasattr(client, "request")
    assert not hasattr(client, "get")
    assert not hasattr(client, "post")
    public = {name for name in dir(client) if not name.startswith("_")}
    assert public == {"costs", "completions", "audio_speeches"}


def test_every_request_is_a_get_to_the_pinned_host(configured) -> None:
    service, _store, _credentials, recorder = configured
    service.sync(force=True)
    assert recorder.requests
    for request in recorder.requests:
        assert request.get_method() == "GET"
        parsed = urllib.request.urlparse(request.full_url)
        assert parsed.scheme == "https"
        assert parsed.hostname == API_HOST
        assert parsed.path in ALLOWED_PATHS
        assert request.data is None


def test_a_path_outside_the_allowlist_is_refused(configured) -> None:
    client = configured[0].client
    with pytest.raises(AccountingError) as refused:
        client._get("/v1/organization/projects", {})
    assert refused.value.code == "path_not_allowed"


@pytest.mark.parametrize(
    "path",
    ["/v1/organization/admin_api_keys", "/v1/organization/projects",
     "/v1/organization/users", "/v1/organization/invites", "/v1/chat/completions"],
)
def test_no_mutation_or_management_endpoint_can_be_named(path: str) -> None:
    assert path not in ALLOWED_PATHS
    body = (SOURCE / "cloud/provider_accounting.py").read_text()
    assert path not in body


def test_a_redirect_is_refused_rather_than_followed() -> None:
    """A 3xx to another host would hand the Admin key to that host."""

    handler = _NoRedirects()
    with pytest.raises(AccountingError) as refused:
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/")
    assert refused.value.code == "redirect_refused"


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (403, "unauthorized"), (429, "rate_limited"),
     (500, "upstream_error"), (503, "upstream_error"), (418, "upstream_status")],
)
def test_upstream_failures_become_safe_codes(tmp_path, status: int, code: str) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    error = urllib.error.HTTPError("https://api.openai.com/v1/organization/costs",
                                   status, "", {}, None)
    client = OpenAIAccountingClient(credentials, opener=Recorder(error=error))
    with pytest.raises(AccountingError) as failed:
        client.costs(start_time=BASE)
    assert failed.value.code == code
    assert SECRET not in failed.value.message


def test_a_failure_message_never_carries_the_key(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    for error in (TimeoutError(), urllib.error.URLError("boom"), OSError("boom")):
        client = OpenAIAccountingClient(credentials, opener=Recorder(error=error))
        with pytest.raises(AccountingError) as failed:
            client.costs(start_time=BASE)
        assert SECRET not in str(failed.value)
        assert "Bearer" not in str(failed.value)


def test_a_malformed_payload_is_refused(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)

    class Bad:
        def __call__(self, request, timeout):
            return b"not json at all"

    client = OpenAIAccountingClient(credentials, opener=Bad())
    with pytest.raises(AccountingError) as failed:
        client.costs(start_time=BASE)
    assert failed.value.code == "malformed_response"


def test_a_response_that_is_not_a_page_is_refused(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)

    class Wrong:
        def __call__(self, request, timeout):
            return json.dumps({"object": "list", "data": []}).encode()

    client = OpenAIAccountingClient(credentials, opener=Wrong())
    with pytest.raises(AccountingError) as failed:
        client.costs(start_time=BASE)
    assert failed.value.code == "malformed_response"


def test_pagination_is_bounded_and_cannot_loop(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)

    class Endless:
        def __call__(self, request, timeout):
            return json.dumps(costs_page(has_more=True, next_page="cursor")).encode()

    client = OpenAIAccountingClient(credentials, opener=Endless())
    with pytest.raises(AccountingError) as failed:
        client.costs(start_time=BASE)
    assert failed.value.code == "too_many_pages"


def test_an_oversized_response_is_refused() -> None:
    assert MAX_RESPONSE_BYTES <= 4 * 1024 * 1024
    body = (SOURCE / "cloud/provider_accounting.py").read_text()
    assert "MAX_RESPONSE_BYTES + 1" in body
    assert "response_too_large" in body


def test_a_missing_credential_is_a_state_not_a_crash(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    client = OpenAIAccountingClient(credentials, opener=Recorder())
    with pytest.raises(AccountingError) as failed:
        client.costs(start_time=BASE)
    assert failed.value.code == "credential_missing"


# ========================== 3. accounting semantics ========================


def test_a_repeated_sync_is_idempotent(configured) -> None:
    service, store, _credentials, _recorder = configured
    service.sync(force=True)
    first = store.cost_between("openai", "organization", BASE, BASE + 10 * DAY)
    for _ in range(3):
        service.sync(force=True)
    assert store.cost_between("openai", "organization", BASE, BASE + 10 * DAY) == first
    with sqlite3.connect(store.database_path) as connection:
        rows = connection.execute(
            "SELECT COUNT(*) FROM provider_accounting_snapshots WHERE endpoint=?", (COSTS,)
        ).fetchone()[0]
    assert rows == 2, "a repeated page must upsert, never accumulate"


def test_a_period_total_is_never_turned_into_per_request_cost(configured) -> None:
    service, store, _credentials, _recorder = configured
    service.sync(force=True)
    body = executable("cloud/provider_accounting.py")
    # No division of a bucket across requests anywhere in the code.
    for forbidden in ("/ len(", "per_request", "apportion", "distribute"):
        assert forbidden not in body, forbidden
    # The stored grain is the provider's own: one row per bucket and line item.
    with sqlite3.connect(store.database_path) as connection:
        grain = connection.execute(
            "SELECT bucket_start, bucket_end FROM provider_accounting_snapshots LIMIT 1"
        ).fetchone()
    assert grain[1] - grain[0] == DAY


def test_provider_sync_never_touches_the_local_ledger(tmp_path) -> None:
    """`provider_usage` is the evidence of what admission actually reserved."""

    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    database = tmp_path / "usage.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE provider_usage (id INTEGER PRIMARY KEY, estimated_cost_usd REAL, cost_basis TEXT);
               INSERT INTO provider_usage (estimated_cost_usd, cost_basis) VALUES (0.123766,'estimated_upper_bound');"""
        )
    store = ProviderAccountingStore(database)
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder()),
        clock=lambda: float(BASE + 2 * DAY),
    )
    service.sync(force=True)
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT estimated_cost_usd, cost_basis FROM provider_usage"
        ).fetchall()
    assert rows == [(0.123766, "estimated_upper_bound")]
    body = (SOURCE / "cloud/provider_accounting.py").read_text()
    assert "UPDATE provider_usage" not in body
    assert "DELETE FROM provider_usage" not in body


def test_provider_reporting_cannot_change_a_budget() -> None:
    """Admission must never consult a figure that lags."""

    body = executable("cloud/provider_accounting.py")
    # The exact identifiers admission is built from. `usage.py` reads these
    # three to decide whether a paid call may proceed; this module must not
    # name any of them.
    for identifier in (
        "daily_budget_usd",
        "monthly_budget_usd",
        "max_estimated_cost_per_request_usd",
        "allow_paid_calls",
        "allow_paid_tts",
        "allow_paid_stt",
        "record_provider_usage",
        "affordable",
    ):
        assert identifier not in body, identifier

    # The admission path is where those live, and it does not know this
    # module exists — so a lagging provider figure cannot reach a decision.
    admission = executable("cloud/usage.py")
    assert "daily_budget_usd" in admission, "the admission check moved; retarget this test"
    for name in ("cloud/usage.py", "pricing.py"):
        assert "provider_accounting" not in imported_modules(name), name
        assert "provider_accounting" not in (SOURCE / name).read_text(), name


def test_costs_are_only_summed_for_whole_buckets_inside_the_window(configured) -> None:
    service, store, _credentials, _recorder = configured
    service.sync(force=True)
    inside = store.cost_between("openai", "organization", BASE, BASE + 2 * DAY)
    assert inside == pytest.approx(0.02)
    # A window that contains no whole bucket reports nothing rather than zero.
    assert store.cost_between("openai", "organization", BASE + 8 * DAY, BASE + 9 * DAY) is None


def test_a_project_scope_is_never_mixed_with_organization_data(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "u.sqlite3")
    moment = {"now": float(BASE + 2 * DAY)}
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder()),
        clock=lambda: moment["now"],
    )
    service.sync(force=True)
    assert store.cost_between("openai", "organization", BASE, BASE + 3 * DAY) is not None
    service.set_project_id("proj_butters12345")
    # Even a manual refresh has a floor, so move past it rather than pretending.
    moment["now"] += 120
    service.sync(force=True)
    # Scoped rows are stored under their own scope, so the two never add up.
    assert store.cost_between("openai", "proj_butters12345", BASE, BASE + 3 * DAY) is not None
    assert service.state()["scope"] == "proj_butters12345"
    assert service.state()["scope_kind"] == "project"


@pytest.mark.parametrize("bad", ["butters-prod", "proj", "proj_", "../proj_x", 7, "proj_$$$"])
def test_a_project_is_identified_only_by_an_exact_id(configured, bad) -> None:
    """No enumeration, no name matching."""

    service = configured[0]
    with pytest.raises(AccountingError) as refused:
        service.set_project_id(bad)
    assert refused.value.code == "invalid_project_id"
    body = (SOURCE / "cloud/provider_accounting.py").read_text()
    assert "/v1/organization/projects" not in body


def test_no_project_means_the_scope_says_organization(configured) -> None:
    service = configured[0]
    assert service.scope() == "organization"
    assert service.state()["scope_kind"] == "organization"


# =========================== 4. freshness / outage =========================


def test_coverage_is_what_came_back_not_what_was_asked_for(configured) -> None:
    """The query reaches past now so the current day is included.

    Reporting that bound as coverage would claim data that does not exist
    yet. The live page said "reported through" a future instant because of
    exactly this confusion.
    """

    service, _store, _credentials, _recorder = configured
    state = service.sync(force=True)
    window = state["reporting_window"]
    now = float(BASE + 2 * DAY)
    # Requested end is deliberately in the future.
    assert window["requested_end"] > now
    # Coverage is bounded by the buckets the provider actually returned.
    assert window["data_from"] == BASE
    assert window["data_through"] == BASE + 2 * DAY
    assert window["data_through"] <= window["requested_end"]


def test_an_open_current_bucket_is_declared(tmp_path) -> None:
    """A daily bucket for today is still filling, and the page must say so."""

    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "u.sqlite3")
    # Mid-day: the newest bucket ends after now.
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder()),
        clock=lambda: float(BASE + DAY + 3600),
    )
    state = service.sync(force=True)
    assert state["reporting_window"]["current_bucket_open"] is True


def test_a_settled_window_is_not_declared_open(configured) -> None:
    service, _store, _credentials, _recorder = configured
    state = service.sync(force=True)
    # Buckets end exactly at `now`, so nothing is still filling.
    assert state["reporting_window"]["current_bucket_open"] is False


def test_the_intended_deployment_needs_only_usage_read() -> None:
    """Usage API Scope: Read, and nothing else.

    Verified against a live key configured that way on 2026-09-21: all three
    reads succeeded with Organization Administration, Audit Logs and
    Fine-tuning all set to None.
    """

    body = executable("cloud/provider_accounting.py")
    # Nothing in the client depends on a surface those scopes would unlock.
    for surface in ("audit_logs", "fine_tuning", "admin_api_keys", "invites",
                    "users", "service_accounts", "rate_limits", "certificates"):
        assert surface not in body, surface
    # And project enumeration is not how the scope is chosen.
    assert "/v1/organization/projects" not in body


def test_state_before_any_sync_says_never_synced(configured) -> None:
    service = configured[0]
    state = service.state()
    assert state["configured"] is True
    assert state["freshness"] == "never_synced"
    assert state["last_success_at"] is None


def test_an_unconfigured_provider_is_a_state_not_an_error(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder())
    )
    state = service.sync(force=True)
    assert state["configured"] is False
    assert state["freshness"] == "not_configured"


def test_a_provider_outage_is_recorded_and_never_raised(tmp_path) -> None:
    """A billing API outage must not become a Butters outage."""

    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    error = urllib.error.HTTPError("https://api.openai.com/x", 503, "", {}, None)
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder(error=error)),
        clock=lambda: float(BASE),
    )
    state = service.sync(force=True)  # must not raise
    assert state["failure_code"] == "upstream_error"
    assert state["freshness"] == "never_synced"


def test_a_failed_sync_does_not_erase_the_last_good_one(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    recorder = Recorder()
    moment = {"now": float(BASE)}
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=recorder),
        clock=lambda: moment["now"],
    )
    service.sync(force=True)
    good = service.state()["last_success_at"]
    assert good is not None

    recorder.error = urllib.error.HTTPError("https://api.openai.com/x", 500, "", {}, None)
    moment["now"] += 120
    state = service.sync(force=True)
    assert state["last_success_at"] == good
    assert state["failure_code"] == "upstream_error"


def test_a_stale_reading_says_so(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    moment = {"now": float(BASE)}
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Recorder()),
        clock=lambda: moment["now"],
    )
    service.sync(force=True)
    assert service.state()["freshness"] == "fresh"
    moment["now"] += 6 * 3600
    assert service.state()["freshness"] == "stale"


def test_the_cache_prevents_hammering_the_billing_api(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    store = ProviderAccountingStore(tmp_path / "usage.sqlite3")
    recorder = Recorder()
    moment = {"now": float(BASE)}
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=recorder),
        clock=lambda: moment["now"],
    )
    service.sync()
    first = len(recorder.requests)
    for _ in range(5):
        service.sync()
    assert len(recorder.requests) == first, "a cached sync must not re-read"
    moment["now"] += 3600
    service.sync()
    assert len(recorder.requests) > first


# ============================== 5. validation ==============================


def test_validation_is_one_bounded_costs_read(configured) -> None:
    service, _store, credentials, recorder = configured
    outcome = service.validate()
    assert outcome["authenticated"] is True
    assert len(recorder.requests) == 1
    parsed = urllib.request.urlparse(recorder.requests[0].full_url)
    assert parsed.path == COSTS
    assert "limit=1" in parsed.query
    # Recorded as metadata, never the key.
    assert credentials.state().last_validated_at is not None
    assert SECRET not in json.dumps(credentials.state().as_dict())


def test_validation_never_lists_or_changes_anything(configured) -> None:
    service, _store, _credentials, recorder = configured
    service.validate()
    for request in recorder.requests:
        assert request.get_method() == "GET"
        assert "users" not in request.full_url
        assert "admin_api_keys" not in request.full_url
        assert "projects" not in request.full_url


def test_a_rejected_key_is_reported_not_raised(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    error = urllib.error.HTTPError("https://api.openai.com/x", 401, "", {}, None)
    service = ProviderAccountingService(
        credentials, ProviderAccountingStore(tmp_path / "u.sqlite3"),
        OpenAIAccountingClient(credentials, opener=Recorder(error=error)),
    )
    outcome = service.validate()
    assert outcome["authenticated"] is False
    assert outcome["code"] == "unauthorized"
    assert "Admin API key" in outcome["detail"]


# ======================== 6. what the endpoints give =======================


def test_audio_speeches_carries_no_cost_which_is_why_tts_cannot_reconcile(configured) -> None:
    """The reason the local TTS figure is a ceiling, asserted against fixtures.

    `/v1/organization/usage/audio_speeches` reports characters and request
    counts and no money at all. Money comes only from `/organization/costs`,
    per day and per line item.
    """

    service, store, _credentials, _recorder = configured
    service.sync(force=True)
    with sqlite3.connect(store.database_path) as connection:
        speech = connection.execute(
            "SELECT reported_cost_usd, characters, num_model_requests FROM "
            "provider_accounting_snapshots WHERE endpoint=?", (AUDIO_SPEECHES,)
        ).fetchone()
    assert speech[0] is None, "the speech usage endpoint reports no cost"
    assert speech[1] == 1200 and speech[2] == 25
    assert "characters" not in str(costs_page())


def test_completions_usage_is_stored_as_quantities(configured) -> None:
    service, store, _credentials, _recorder = configured
    service.sync(force=True)
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT line_item, input_tokens, output_tokens, num_model_requests, reported_cost_usd "
            "FROM provider_accounting_snapshots WHERE endpoint=?", (COMPLETIONS,)
        ).fetchone()
    assert row[0] == "gpt-5.6-terra"
    assert row[1] == 386 and row[2] == 69 and row[3] == 2
    assert row[4] is None


def test_an_empty_provider_result_is_handled(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    empty = {"object": "page", "data": [], "has_more": False, "next_page": None}
    recorder = Recorder({COSTS: empty, COMPLETIONS: empty, AUDIO_SPEECHES: empty})
    store = ProviderAccountingStore(tmp_path / "u.sqlite3")
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=recorder),
        clock=lambda: float(BASE),
    )
    state = service.sync(force=True)
    assert state["freshness"] == "fresh"
    assert state["cost_by_line_item"] == []


def test_costs_pagination_collects_every_bucket_once(tmp_path) -> None:
    credentials = UsageAdminCredentialStore(tmp_path)
    credentials.store(SECRET)
    pages = [costs_page(buckets=2, has_more=True, next_page="second"), costs_page(buckets=2)]

    class Paged:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, request, timeout):
            path = urllib.request.urlparse(request.full_url).path
            if path != COSTS:
                return json.dumps({"object": "page", "data": [], "has_more": False, "next_page": None}).encode()
            page = pages[min(self.calls, 1)]
            self.calls += 1
            return json.dumps(page).encode()

    store = ProviderAccountingStore(tmp_path / "u.sqlite3")
    service = ProviderAccountingService(
        credentials, store, OpenAIAccountingClient(credentials, opener=Paged()),
        clock=lambda: float(BASE),
    )
    service.sync(force=True)
    with sqlite3.connect(store.database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM provider_accounting_snapshots WHERE endpoint=?", (COSTS,)
        ).fetchone()[0]
    # Both pages carried the same two buckets; the key is the bucket, so the
    # repeat upserts rather than double-counting.
    assert count == 2
