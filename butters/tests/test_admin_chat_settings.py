"""Admin chat settings must govern the request Butters actually sends.

As with the voice, a stored row proves nothing. These tests change the model
and the request controls in Admin and then inspect the JSON body that would
leave the process for the OpenAI Responses API.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
from butters.ai.credentials import OpenAICredentialStore
from butters.assistant_config import load_assistant_settings
from butters.cloud.general import OpenAIGeneralReasoner
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

SENTINEL = "sk-butters-sentinel-CHAT-0123456789abcdefgh"


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


class ReasoningRecorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, request, **_kwargs):
        self.requests.append(json.loads(request.data))
        return _Response(
            json.dumps(
                {
                    "id": "resp_1",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Box three sits a little more humid.",
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 20, "output_tokens": 12},
                }
            ).encode()
        )

    @property
    def last(self) -> dict[str, object]:
        return self.requests[-1]


def _application(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        cloud=replace(
            base.cloud,
            enabled=True,
            allow_paid_calls=True,
            max_estimated_cost_per_request_usd=50.0,
            daily_budget_usd=100.0,
            monthly_budget_usd=100.0,
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
    service = BetaAssistantService(settings, vocabulary, state_dir=tmp_path)
    recorder = ReasoningRecorder()
    service.ai.credentials = OpenAICredentialStore(tmp_path, environment={})
    service.ai.credentials.store(SENTINEL, validation=None)
    service.ai._chat_factory = lambda key: OpenAIGeneralReasoner(
        settings.cloud, api_key=key or "", opener=recorder
    )
    service.ai._activate(force=True)
    app = create_app(settings, vocabulary, service, stt_engine_factory=Engine)
    return app, service, recorder


def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _mutation_headers(http):
    headers = {"tailscale-user-login": "admin@example.com"}
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _cloud_turn(http, headers):
    """One ordinary Butters Chat turn that the router sends to the cloud.

    This is the real user path, not an administrator override, so the model
    and effort in the outgoing request are the configured ones.
    """

    response = await http.post(
        "/api/chat",
        headers=headers,
        json={"text": "why might box three stay more humid than the others"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _the_configured_model_is_the_model_requested(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.6-luna", "reasoning_effort": "low"},
        )
        await _cloud_turn(http, headers)
        first = recorder.last

        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.6-sol", "reasoning_effort": "xhigh"},
        )
        await _cloud_turn(http, headers)
        second = recorder.last

    assert first["model"] == "gpt-5.6-luna"
    assert first["reasoning"]["effort"] == "low"
    assert second["model"] == "gpt-5.6-sol"
    assert second["reasoning"]["effort"] == "xhigh"


def test_the_configured_model_is_the_model_requested(tmp_path, monkeypatch) -> None:
    asyncio.run(_the_configured_model_is_the_model_requested(tmp_path, monkeypatch))


async def _configured_advanced_controls_reach_the_request(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "verbosity": "low",
                "max_output_tokens": 800,
                "truncation": "auto",
                "parallel_tool_calls": True,
                "max_tool_calls": 3,
                "store_responses": False,
                "prompt_cache_enabled": True,
            },
        )
        await _cloud_turn(http, headers)

    body = recorder.last
    assert body["text"] == {"verbosity": "low"}
    assert body["truncation"] == "auto"
    assert body["parallel_tool_calls"] is True
    assert body["max_tool_calls"] == 3
    assert body["store"] is False
    assert body["prompt_cache_key"]
    assert body["max_output_tokens"] == 800


def test_configured_advanced_controls_reach_the_request(tmp_path, monkeypatch) -> None:
    asyncio.run(_configured_advanced_controls_reach_the_request(tmp_path, monkeypatch))


async def _unset_controls_are_omitted_from_the_request(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.6-terra"},
        )
        await _cloud_turn(http, headers)

    body = recorder.last
    for omitted in ("text", "temperature", "top_p", "truncation", "max_tool_calls", "prompt_cache_key"):
        assert omitted not in body, omitted


def test_unset_controls_are_omitted_from_the_request(tmp_path, monkeypatch) -> None:
    asyncio.run(_unset_controls_are_omitted_from_the_request(tmp_path, monkeypatch))


async def _an_unsupported_sampling_control_never_reaches_the_provider(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        rejected = await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "temperature": 0.7,
            },
        )
        await _cloud_turn(http, headers)

    assert rejected.status_code == 400
    assert rejected.json()["error"] == "unsupported_parameter"
    assert "temperature" not in recorder.last


def test_an_unsupported_sampling_control_never_reaches_the_provider(tmp_path, monkeypatch) -> None:
    asyncio.run(_an_unsupported_sampling_control_never_reaches_the_provider(tmp_path, monkeypatch))


async def _an_invalid_model_is_rejected_and_never_silently_substituted(tmp_path, monkeypatch):
    app, service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={"provider": "openai", "model": "gpt-5.6-luna"},
        )
        rejected = await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={"provider": "openai", "model": "gpt-4.1-legacy"},
        )
        await _cloud_turn(http, headers)

    assert rejected.status_code == 400
    assert rejected.json()["error"] == "model_denied"
    # The previous valid model is still the one in force.
    assert service.ai.effective.chat.model == "gpt-5.6-luna"
    assert recorder.last["model"] == "gpt-5.6-luna"


def test_an_invalid_model_is_rejected_and_never_silently_substituted(tmp_path, monkeypatch) -> None:
    asyncio.run(_an_invalid_model_is_rejected_and_never_silently_substituted(tmp_path, monkeypatch))


async def _an_administrator_override_does_not_inherit_another_models_parameters(
    tmp_path, monkeypatch
):
    """A forced model must not receive controls saved for a different one."""

    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "verbosity": "high",
                "max_tool_calls": 2,
            },
        )
        forced = await http.post(
            "/api/admin/routing/test",
            headers=headers,
            json={
                "text": "why might box three stay more humid",
                "override": "force_cloud_model",
                "model": "gpt-5.6-sol",
            },
        )
        assert forced.status_code == 200, forced.text

    assert recorder.last["model"] == "gpt-5.6-sol"
    assert "text" not in recorder.last
    assert "max_tool_calls" not in recorder.last


def test_an_administrator_override_does_not_inherit_another_models_parameters(
    tmp_path, monkeypatch
) -> None:
    asyncio.run(
        _an_administrator_override_does_not_inherit_another_models_parameters(
            tmp_path, monkeypatch
        )
    )


async def _the_catalog_endpoint_drives_the_admin_dropdowns(tmp_path, monkeypatch):
    app, _service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        catalog = (await http.get("/api/admin/ai/catalog", headers=headers)).json()

    providers = {item["id"] for item in catalog["chat_providers"]}
    openai = next(item for item in catalog["chat_providers"] if item["id"] == "openai")
    speech = {item["id"] for item in catalog["speech_providers"]}

    assert providers == {"openai"}
    assert speech == {"openai", "local"}
    assert [item["id"] for item in openai["chat_models"]] == [
        "gpt-5.6-luna",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
    ]
    assert catalog["max_output_tokens"] >= 64


def test_the_catalog_endpoint_drives_the_admin_dropdowns(tmp_path, monkeypatch) -> None:
    asyncio.run(_the_catalog_endpoint_drives_the_admin_dropdowns(tmp_path, monkeypatch))


async def _the_credential_never_appears_in_a_settings_or_catalog_response(
    tmp_path, monkeypatch
):
    app, _service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        documents = [
            (await http.get("/api/admin/ai/catalog", headers=headers)).text,
            (await http.get("/api/admin/ai/settings", headers=headers)).text,
            (
                await http.post(
                    "/api/admin/ai/chat",
                    headers=headers,
                    json={"provider": "openai", "model": "gpt-5.6-terra"},
                )
            ).text,
        ]

    for document in documents:
        assert SENTINEL not in document


def test_the_credential_never_appears_in_a_settings_or_catalog_response(
    tmp_path, monkeypatch
) -> None:
    asyncio.run(
        _the_credential_never_appears_in_a_settings_or_catalog_response(tmp_path, monkeypatch)
    )
