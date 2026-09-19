"""Administrator management of the one OpenAI credential.

Every test here uses a synthetic sentinel value. The live production key is
never read, copied, or referenced by this suite.

The invariants under test:

* replacement is validate-then-swap, so a bad candidate cannot displace a
  working credential;
* the secret never appears in a response, a log record, an audit entry, a job
  payload, or an exception;
* mutation requires an administrator identity, an explicit confirmation, and a
  FRESH passkey assertion bound to the exact operation;
* removing the credential removes Butters' copy only and says so.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from butters.ai.credentials import (
    OpenAICredentialStore,
    OpenAICredentialValidator,
    normalize_candidate,
)
from butters.assistant_config import load_assistant_settings
from butters.auth.manager import AuthenticationVerification
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

# Recognizable, synthetic, and never a real credential.
SENTINEL = "sk-butters-sentinel-DO-NOT-LEAK-0123456789abcdef"
REPLACEMENT = "sk-butters-sentinel-SECOND-fedcba9876543210zz"


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


class _Response:
    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class Upstream:
    """Records what was asked of OpenAI and answers as configured."""

    def __init__(self, *, accept: set[str] | None = None, missing_model: bool = False) -> None:
        self.accept = {SENTINEL} if accept is None else accept
        self.missing_model = missing_model
        self.paths: list[str] = []
        self.calls = 0

    def __call__(self, request, **_kwargs):
        self.calls += 1
        self.paths.append(request.full_url)
        token = request.headers["Authorization"].removeprefix("Bearer ")
        if token not in self.accept:
            # The real API echoes the submitted key in its error body.
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                f"Incorrect API key provided: {token}",
                {},
                None,
            )
        if self.missing_model and "/v1/models/" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "no such model", {}, None)
        return _Response(b'{"object":"list","data":[]}')


def _application(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
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
    upstream = Upstream()
    service.ai.validator = OpenAICredentialValidator(
        settings.cloud.base_url, opener=upstream
    )
    service.ai.credentials = OpenAICredentialStore(tmp_path, environment={})
    app = create_app(settings, vocabulary, service, stt_engine_factory=Engine)
    return app, service, upstream


def _credential_assertion() -> dict[str, object]:
    encoded = base64.urlsafe_b64encode(b"credential-one").rstrip(b"=").decode()
    return {"id": encoded, "uv": True}


async def _mutation_headers(http, identity: str = "admin@example.com"):
    headers = {"tailscale-user-login": identity}
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _fresh_grant(http, headers, subject: str) -> str:
    begin = await http.post(
        "/api/auth/authenticate/options",
        headers=headers,
        json={"purpose": "openai_credential", "subject": subject},
    )
    assert begin.status_code == 200, begin.text
    verified = await http.post(
        "/api/auth/authenticate/verify",
        headers=headers,
        json={
            "ceremony_id": begin.json()["ceremony_id"],
            "credential": _credential_assertion(),
        },
    )
    assert verified.status_code == 200, verified.text
    return verified.json()["fresh_grant"]


def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


# ------------------------------- state --------------------------------------


async def _test_not_configured_is_reported_without_inventing_a_credential(
    tmp_path: Path, monkeypatch
) -> None:
    app, _service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        response = await http.get("/api/admin/integrations/openai", headers=headers)
        state = response.json()

    assert response.status_code == 200
    assert state["configured"] is False
    assert state["source"] == "none"
    assert state["fingerprint"] is None


async def _test_valid_candidate_is_stored_validated_and_activated(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        grant = await _fresh_grant(http, headers, "set")
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": grant, "confirm": True},
        )
        body = response.json()

    assert response.status_code == 200, body
    assert body["replaced"] is True
    assert body["validation"]["authenticated"] is True
    assert body["credential"]["configured"] is True
    assert body["credential"]["source"] == "butters_store"
    # Validation authenticates and asks about the model separately.
    assert body["validation"]["model_checked"] == service.ai.effective.chat.model
    assert body["validation"]["model_available"] is True
    assert any(path.endswith("/v1/models") for path in upstream.paths)
    # The live provider was reinitialized with the credential, not just stored.
    assert service.ai.effective.credential_fingerprint is not None
    assert service.cloud_tts._api_key == SENTINEL


async def _test_stored_credential_file_is_private_and_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        grant = await _fresh_grant(http, headers, "set")
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": grant, "confirm": True},
        )

    path = service.ai.credentials.key_path
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    # No temporary file survived the write.
    assert [item.name for item in sorted(path.parent.iterdir()) if item.name.startswith(".")] == []


async def _test_invalid_candidate_leaves_the_existing_credential_active(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        rejected = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": "sk-butters-sentinel-WRONG-000000000000000000",
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        body = rejected.json()

    assert rejected.status_code == 200, body
    assert body["replaced"] is False
    assert body["validation"]["authenticated"] is False
    assert body["validation"]["code"] == "unauthorized"
    # The working credential is untouched, on disk and in the live provider.
    assert service.ai.credentials.key_path.read_text() == SENTINEL
    assert service.cloud_tts._api_key == SENTINEL
    assert upstream.calls >= 2


async def _test_replacement_swaps_the_credential_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, upstream = _application(tmp_path, monkeypatch)
    upstream.accept = {SENTINEL, REPLACEMENT}
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        first = service.ai.credentials.state().fingerprint
        replaced = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": REPLACEMENT,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )

    assert replaced.json()["replaced"] is True
    assert service.ai.credentials.key_path.read_text() == REPLACEMENT
    assert service.ai.credentials.state().fingerprint != first
    assert service.cloud_tts._api_key == REPLACEMENT


async def _test_activation_failure_rolls_back_to_the_previous_credential(
    tmp_path: Path, monkeypatch
) -> None:
    """A validated candidate that cannot be activated is not reported as live."""

    app, service, upstream = _application(tmp_path, monkeypatch)
    upstream.accept = {SENTINEL, REPLACEMENT}
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        working = service.cloud_tts

        failures = {"count": 0}

        def broken(api_key):
            failures["count"] += 1
            if api_key == REPLACEMENT:
                raise RuntimeError("speech provider could not be initialized")
            return working

        service.ai._speech_factory = broken
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": REPLACEMENT,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        body = response.json()

    assert body["replaced"] is False
    assert body["activation_error"]["code"] == "activation_failed"
    # Validation succeeded, so the failure must not be reported as a bad key.
    assert body["validation"]["authenticated"] is True
    assert service.ai.credentials.key_path.read_text() == SENTINEL
    assert service.cloud_tts is working


async def _test_removal_is_local_only_and_says_so(tmp_path: Path, monkeypatch) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        response = await http.request(
            "DELETE",
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "fresh_grant": await _fresh_grant(http, headers, "remove"),
                "confirm": True,
            },
        )
        body = response.json()

    assert body["removed"] is True
    assert body["upstream_revoked"] is False
    assert "OpenAI Platform" in body["notice"]
    assert "revoked" not in body["notice"].casefold().replace("revoke it", "")
    assert not service.ai.credentials.key_path.exists()
    assert service.ai.credentials.state().configured is False


async def _test_test_credential_separates_authentication_from_model_access(
    tmp_path: Path, monkeypatch
) -> None:
    app, _service, upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )
        upstream.missing_model = True
        response = await http.post(
            "/api/admin/integrations/openai/test", headers=headers, json={}
        )
        body = response.json()

    assert body["validation"]["authenticated"] is True
    assert body["validation"]["model_available"] is False
    # Validation only ever performs bounded GETs; it creates nothing upstream.
    assert all("/v1/models" in path for path in upstream.paths)


async def _test_validation_creates_no_upstream_object(tmp_path: Path, monkeypatch) -> None:
    app, service, upstream = _application(tmp_path, monkeypatch)
    methods: list[str] = []

    def recording(request, **kwargs):
        methods.append(request.get_method())
        return upstream(request, **kwargs)

    service.ai.validator = OpenAICredentialValidator(
        "https://api.openai.com", opener=recording
    )
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )

    assert methods and set(methods) == {"GET"}


# --------------------------- authorization matrix ---------------------------


async def _test_anonymous_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, _service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        await http.get("/api/session")
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers={"origin": "http://testserver"},
            json={"api_key": SENTINEL, "fresh_grant": "x", "confirm": True},
        )

    assert response.status_code == 403
    assert response.json()["error"] != "fresh_required"


async def _test_normal_authenticated_user_is_rejected(tmp_path: Path, monkeypatch) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http, identity="partner@example.com")
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": "x", "confirm": True},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "admin_identity_denied"
    assert not service.ai.credentials.key_path.exists()


async def _test_administrator_without_fresh_authentication_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        missing = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "confirm": True},
        )
        forged = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": "not-a-grant", "confirm": True},
        )

    assert missing.status_code == 403 and missing.json()["error"] == "fresh_required"
    assert forged.status_code in {403, 409}
    assert forged.json()["error"] == "fresh_grant_denied"
    assert not service.ai.credentials.key_path.exists()


async def _test_a_grant_for_removal_cannot_authorize_a_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        grant = await _fresh_grant(http, headers, "remove")
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": grant, "confirm": True},
        )

    assert response.json()["error"] == "fresh_binding_denied"
    assert not service.ai.credentials.key_path.exists()


async def _test_a_grant_is_single_use(tmp_path: Path, monkeypatch) -> None:
    app, _service, upstream = _application(tmp_path, monkeypatch)
    upstream.accept = {SENTINEL}
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        grant = await _fresh_grant(http, headers, "set")
        first = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": grant, "confirm": True},
        )
        replayed = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "fresh_grant": grant, "confirm": True},
        )

    assert first.json()["replaced"] is True
    assert replayed.json()["error"] == "fresh_grant_denied"


async def _test_confirmation_is_required(tmp_path: Path, monkeypatch) -> None:
    app, service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": False,
            },
        )

    assert response.json()["error"] == "confirmation_required"
    assert not service.ai.credentials.key_path.exists()


async def _test_csrf_and_origin_are_enforced(tmp_path: Path, monkeypatch) -> None:
    app, _service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        without_csrf = {key: value for key, value in headers.items() if key != "x-butters-csrf"}
        no_csrf = await http.post(
            "/api/admin/integrations/openai/key", headers=without_csrf, json={}
        )
        without_origin = {key: value for key, value in headers.items() if key != "origin"}
        no_origin = await http.post(
            "/api/admin/integrations/openai/key", headers=without_origin, json={}
        )

    assert no_csrf.json()["error"] == "csrf_denied"
    assert no_origin.json()["error"] == "origin_missing"


async def _test_the_endpoint_refuses_unknown_fields(tmp_path: Path, monkeypatch) -> None:
    app, _service, _upstream = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={"api_key": SENTINEL, "confirm": True, "path": "/etc/butters/x"},
        )

    assert response.status_code == 400
    assert "unsupported fields" in response.json()["message"]


# ------------------------------ leak checks ---------------------------------


async def _test_the_sentinel_never_reaches_a_response_log_audit_or_job(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    app, service, upstream = _application(tmp_path, monkeypatch)
    upstream.accept = {REPLACEMENT}
    caplog.set_level(logging.DEBUG)
    bodies: list[str] = []
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        # One rejected candidate and one accepted candidate: both must vanish.
        for candidate, subject in ((SENTINEL, "set"), (REPLACEMENT, "set")):
            response = await http.post(
                "/api/admin/integrations/openai/key",
                headers=headers,
                json={
                    "api_key": candidate,
                    "fresh_grant": await _fresh_grant(http, headers, subject),
                    "confirm": True,
                },
            )
            bodies.append(response.text)
        bodies.append((await http.get("/api/admin/integrations/openai", headers=headers)).text)
        bodies.append((await http.get("/api/admin/ai/settings", headers=headers)).text)
        bodies.append((await http.get("/api/admin/models", headers=headers)).text)
        bodies.append((await http.get("/api/admin/security", headers=headers)).text)
        bodies.append((await http.get("/api/admin/system", headers=headers)).text)
        bodies.append((await http.get("/api/admin/actions", headers=headers)).text)
        bodies.append((await http.get("/api/admin/logs", headers=headers)).text)
        bodies.append((await http.get("/api/admin/traces", headers=headers)).text)

    audit = json.dumps(service.action_state.audit_entries(100))
    jobs = json.dumps(service.action_state.jobs(identity="identity:admin@example.com", limit=50))
    logs = "\n".join(record.getMessage() for record in caplog.records)

    for secret in (SENTINEL, REPLACEMENT):
        for document in bodies:
            assert secret not in document
        assert secret not in audit
        assert secret not in jobs
        assert secret not in logs
    # The audit still records that the operation happened, by fingerprint.
    assert "ai.credential.set" in audit


async def _test_an_upstream_error_body_quoting_the_key_is_not_re_exposed(
    tmp_path: Path, monkeypatch
) -> None:
    """The real API echoes a rejected key back; that body must not escape."""

    app, _service, upstream = _application(tmp_path, monkeypatch)
    upstream.accept = set()
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        response = await http.post(
            "/api/admin/integrations/openai/key",
            headers=headers,
            json={
                "api_key": SENTINEL,
                "fresh_grant": await _fresh_grant(http, headers, "set"),
                "confirm": True,
            },
        )

    assert SENTINEL not in response.text
    assert response.json()["validation"]["detail"] == "OpenAI rejected the credential"


def test_a_malformed_candidate_is_never_quoted_back() -> None:
    from butters.ai.credentials import CredentialError

    for candidate in ("sk short", SENTINEL + " trailing", "é" * 40):
        with pytest.raises(CredentialError) as denied:
            normalize_candidate(candidate)
        assert candidate not in str(denied.value)
        assert SENTINEL not in str(denied.value)


def test_store_repr_and_state_never_carry_the_secret(tmp_path: Path) -> None:
    store = OpenAICredentialStore(tmp_path, environment={})
    store.store(SENTINEL, validation=None)
    state = json.dumps(store.state().as_dict())

    assert SENTINEL not in repr(store)
    assert SENTINEL not in state
    assert store.state().fingerprint and store.state().fingerprint not in SENTINEL


def test_stored_credential_takes_precedence_over_the_unit_environment(tmp_path: Path) -> None:
    """The deployed env var remains a fallback; it is never read or rewritten."""

    environment = {"OPENAI_API_KEY": "sk-butters-sentinel-FROM-UNIT-ENVIRONMENT-01"}
    store = OpenAICredentialStore(tmp_path, environment=environment)

    assert store.source() == "unit_environment"
    assert store.secret() == environment["OPENAI_API_KEY"]

    store.store(SENTINEL, validation=None)
    assert store.source() == "butters_store"
    assert store.secret() == SENTINEL

    store.remove()
    assert store.source() == "unit_environment"
    # Removal touched Butters' own copy only.
    assert environment["OPENAI_API_KEY"] == "sk-butters-sentinel-FROM-UNIT-ENVIRONMENT-01"


# --- synchronous entry points (the suite runs on asyncio only) ---


def test_not_configured_is_reported_without_inventing_a_credential(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_not_configured_is_reported_without_inventing_a_credential(tmp_path, monkeypatch))


def test_valid_candidate_is_stored_validated_and_activated(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_valid_candidate_is_stored_validated_and_activated(tmp_path, monkeypatch))


def test_stored_credential_file_is_private_and_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_stored_credential_file_is_private_and_atomic(tmp_path, monkeypatch))


def test_invalid_candidate_leaves_the_existing_credential_active(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_invalid_candidate_leaves_the_existing_credential_active(tmp_path, monkeypatch))


def test_replacement_swaps_the_credential_atomically(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_replacement_swaps_the_credential_atomically(tmp_path, monkeypatch))


def test_activation_failure_rolls_back_to_the_previous_credential(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_activation_failure_rolls_back_to_the_previous_credential(tmp_path, monkeypatch))


def test_removal_is_local_only_and_says_so(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_removal_is_local_only_and_says_so(tmp_path, monkeypatch))


def test_test_credential_separates_authentication_from_model_access(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_test_credential_separates_authentication_from_model_access(tmp_path, monkeypatch))


def test_validation_creates_no_upstream_object(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_validation_creates_no_upstream_object(tmp_path, monkeypatch))


def test_anonymous_is_rejected(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_anonymous_is_rejected(tmp_path, monkeypatch))


def test_normal_authenticated_user_is_rejected(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_normal_authenticated_user_is_rejected(tmp_path, monkeypatch))


def test_administrator_without_fresh_authentication_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_administrator_without_fresh_authentication_is_rejected(tmp_path, monkeypatch))


def test_a_grant_for_removal_cannot_authorize_a_replacement(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_a_grant_for_removal_cannot_authorize_a_replacement(tmp_path, monkeypatch))


def test_a_grant_is_single_use(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_a_grant_is_single_use(tmp_path, monkeypatch))


def test_confirmation_is_required(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_confirmation_is_required(tmp_path, monkeypatch))


def test_csrf_and_origin_are_enforced(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_csrf_and_origin_are_enforced(tmp_path, monkeypatch))


def test_the_endpoint_refuses_unknown_fields(tmp_path: Path, monkeypatch) -> None:
    asyncio.run(_test_the_endpoint_refuses_unknown_fields(tmp_path, monkeypatch))


def test_the_sentinel_never_reaches_a_response_log_audit_or_job(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    asyncio.run(_test_the_sentinel_never_reaches_a_response_log_audit_or_job(tmp_path, monkeypatch, caplog))


def test_an_upstream_error_body_quoting_the_key_is_not_re_exposed(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(_test_an_upstream_error_body_quoting_the_key_is_not_re_exposed(tmp_path, monkeypatch))
