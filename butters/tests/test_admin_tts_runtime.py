"""Admin TTS settings must govern the speech Butters Chat actually produces.

The central test in this file is deliberately end to end. It does not assert
that a database row changed; it changes the voice in Admin, asks Butters Chat
a question, requests the spoken answer, and inspects the synthesis request
that left the process. It does this for two distinct voices.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import httpx
from butters.ai.credentials import OpenAICredentialStore
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService
from butters.web.speech import OpenAITTSProvider

SENTINEL = "sk-butters-sentinel-TTS-0123456789abcdefghij"


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


class Sensors:
    def snapshot(self):
        return SensorSnapshot(
            "now",
            (
                SensorRecord(
                    "environment", "3", "now", 1, "online", {"humidity": 42.0}
                ),
            ),
        )


class Health:
    def snapshot(self):
        return ServerHealthSnapshot(1, 0, 0, 0, 1, 0, 1, 1, 40, "0x0", ())


class SynthesisRecorder:
    """Captures each Realtime synthesis exchange that would leave the process."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def __call__(self, url, **kwargs):
        return _RealtimeConnection(self, url, kwargs)

    @property
    def last(self) -> dict[str, object]:
        return self.requests[-1]


class _RealtimeConnection:
    def __init__(self, recorder: SynthesisRecorder, url: str, kwargs) -> None:
        self.recorder = recorder
        self.current: dict[str, object] = {"url": url, "headers": kwargs["additional_headers"]}
        self.events = [json.dumps({"type": "session.created"})]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def recv(self, *, timeout: float) -> str:
        assert timeout > 0
        return self.events.pop(0)

    def send(self, message: str) -> None:
        event = json.loads(message)
        if event["type"] == "session.update":
            session = event["session"]
            self.current.update(
                model=session["model"],
                voice=session["audio"]["output"]["voice"],
                instructions=session["instructions"],
            )
            self.events.append(json.dumps({"type": "session.updated"}))
        elif event["type"] == "conversation.item.create":
            self.current["input"] = event["item"]["content"][0]["text"]
        elif event["type"] == "response.create":
            self.recorder.requests.append(dict(self.current))
            self.events.extend(
                [
                    json.dumps(
                        {
                            "type": "response.output_audio.delta",
                            "delta": base64.b64encode(b"\0\0" * 2400).decode(),
                        }
                    ),
                    json.dumps(
                        {
                            "type": "response.done",
                            "response": {
                                "status": "completed",
                                "usage": {
                                    "input_token_details": {"text_tokens": 12},
                                    "output_token_details": {"audio_tokens": 20},
                                },
                            },
                        }
                    ),
                ]
            )


def _application(tmp_path: Path, monkeypatch, *, paid_tts: bool = True):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        providers=replace(
            base.providers,
            allow_paid_tts=paid_tts,
            cloud_tts_price_per_million_characters_usd=15.0,
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
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=NoCloud(),
        state_dir=tmp_path,
    )
    recorder = SynthesisRecorder()
    service.ai.credentials = OpenAICredentialStore(tmp_path, environment={})
    service.ai.credentials.store(SENTINEL, validation=None)
    # Rebuild the cloud speech provider through the same factory the runtime
    # uses, so the recorder sits exactly where the real Realtime call would.
    service.ai._speech_factory = lambda key: OpenAITTSProvider(
        settings, api_key=key or "", registry=service.ai_registry, connector=recorder
    )
    service.ai._activate(force=True)
    app = create_app(settings, vocabulary, service, stt_engine_factory=Engine)
    return app, service, recorder


def _client(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


async def _mutation_headers(http, identity: str = "admin@example.com"):
    headers = {"tailscale-user-login": identity}
    session = (await http.get("/api/session", headers=headers)).json()
    return {
        **headers,
        "origin": "http://testserver",
        "x-butters-csrf": session["csrf_token"],
    }


async def _set_voice(http, headers, **payload):
    body = {
        "provider": "openai_realtime",
        "model": "gpt-realtime-2.1-mini",
        **payload,
    }
    return await http.post("/api/admin/ai/tts", headers=headers, json=body)


async def _spoken_chat_answer(http, headers) -> httpx.Response:
    """A real Butters Chat turn followed by its real spoken response."""

    answer = await http.post(
        "/api/chat",
        headers=headers,
        json={"text": "what is the humidity in box three"},
    )
    assert answer.status_code == 200, answer.text
    spoken = await http.post(
        "/api/speech", headers=headers, json={"trace_id": answer.json()["trace_id"]}
    )
    assert spoken.status_code == 200, spoken.text
    return spoken


# ==================== the wiring proof: voice -> synthesis ==================


async def _changing_the_admin_voice_changes_real_chat_synthesis(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)

        assert (await _set_voice(http, headers, voice="cedar")).status_code == 200
        await _spoken_chat_answer(http, headers)
        first = recorder.last

        assert (await _set_voice(http, headers, voice="marin")).status_code == 200
        await _spoken_chat_answer(http, headers)
        second = recorder.last

    # Two distinct voices, two distinct synthesis requests actually issued.
    assert len(recorder.requests) == 2
    assert first["voice"] == "cedar"
    assert second["voice"] == "marin"
    assert first["model"] == second["model"] == "gpt-realtime-2.1-mini"


def test_changing_the_admin_voice_changes_real_chat_synthesis(tmp_path, monkeypatch) -> None:
    asyncio.run(_changing_the_admin_voice_changes_real_chat_synthesis(tmp_path, monkeypatch))


async def _style_reaches_the_synthesis_request(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="sage", instructions="Brisk and dry.")
        await _spoken_chat_answer(http, headers)

    assert recorder.last["voice"] == "sage"
    assert "Speaking style: Brisk and dry." in recorder.last["instructions"]
    assert "speed" not in recorder.last


def test_style_reaches_the_synthesis_request(tmp_path, monkeypatch) -> None:
    asyncio.run(_style_reaches_the_synthesis_request(tmp_path, monkeypatch))


async def _classic_model_rejects_unsupported_style_without_changing_effective_tts(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="sage")
        rejected = await http.post(
            "/api/admin/ai/tts",
            headers=headers,
            json={
                "provider": "openai",
                "model": "tts-1",
                "voice": "alloy",
                "instructions": "Brisk and dry.",
            },
        )
        await _spoken_chat_answer(http, headers)

    assert rejected.status_code == 400
    assert rejected.json()["error"] == "unsupported_parameter"
    assert recorder.last["model"] == "gpt-realtime-2.1-mini"
    assert recorder.last["voice"] == "sage"


def test_classic_model_rejects_unsupported_style_without_changing_effective_tts(
    tmp_path, monkeypatch
) -> None:
    asyncio.run(
        _classic_model_rejects_unsupported_style_without_changing_effective_tts(
            tmp_path, monkeypatch
        )
    )


async def _a_voice_outside_the_model_catalog_is_refused(tmp_path, monkeypatch):
    app, service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="cedar")
        wrong_model = await _set_voice(http, headers, model="tts-1", voice="cedar")
        unknown = await _set_voice(http, headers, voice="Cedar")
        await _spoken_chat_answer(http, headers)

    assert wrong_model.status_code == 400
    assert wrong_model.json()["error"] == "model_denied"
    assert unknown.json()["error"] == "invalid_voice"
    # A refused change never becomes effective, and synthesis keeps the old one.
    assert service.ai.effective.speech.voice == "cedar"
    assert recorder.last["voice"] == "cedar"


def test_a_voice_outside_the_model_catalog_is_refused(tmp_path, monkeypatch) -> None:
    asyncio.run(_a_voice_outside_the_model_catalog_is_refused(tmp_path, monkeypatch))


async def _speed_outside_the_model_range_is_refused(tmp_path, monkeypatch):
    app, _service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        too_fast = await _set_voice(http, headers, voice="cedar", speed=9.0)
        local_too_fast = await http.post(
            "/api/admin/ai/tts",
            headers=headers,
            json={
                "provider": "local",
                "model": "local-piper",
                "voice": "kathleen",
                "speed": 3.0,
            },
        )

    assert too_fast.json()["error"] == "unsupported_parameter"
    assert local_too_fast.json()["error"] == "invalid_speed"


def test_speed_outside_the_model_range_is_refused(tmp_path, monkeypatch) -> None:
    asyncio.run(_speed_outside_the_model_range_is_refused(tmp_path, monkeypatch))


# ------------------------------- preview ------------------------------------


async def _preview_uses_the_same_backend_as_chat(tmp_path, monkeypatch):
    app, _service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="ballad")
        preview = await http.post(
            "/api/admin/voice/preview",
            headers=headers,
            json={
                "name": "preview",
                "provider": "openai_realtime",
                "model": "gpt-realtime-2.1-mini",
                "voice": "ballad",
                "speed": 1.0,
                "phrase": "Hello. This is Butters.",
            },
        )
        previewed = recorder.last
        await _spoken_chat_answer(http, headers)
        spoken = recorder.last

    assert preview.status_code == 200
    assert preview.headers["content-type"] == "audio/wav"
    assert preview.headers["X-Butters-TTS-Provider"] == "openai_realtime"
    # One synthesis stack: the preview and the chat answer differ only in text.
    assert previewed["model"] == spoken["model"]
    assert previewed["voice"] == spoken["voice"] == "ballad"
    assert "speed" not in previewed and "speed" not in spoken
    assert previewed["input"] == "Hello. This is Butters."


def test_preview_uses_the_same_backend_as_chat(tmp_path, monkeypatch) -> None:
    asyncio.run(_preview_uses_the_same_backend_as_chat(tmp_path, monkeypatch))


async def _preview_is_bounded_authorized_and_rate_limited(tmp_path, monkeypatch):
    app, _service, _recorder = _application(tmp_path, monkeypatch)
    body = {
        "name": "preview",
        "provider": "openai_realtime",
        "model": "gpt-realtime-2.1-mini",
        "voice": "cedar",
        "speed": 1.0,
        "phrase": "Hello.",
    }
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        anonymous = await http.post(
            "/api/admin/voice/preview",
            headers={"origin": "http://testserver"},
            json=body,
        )
        too_long = await http.post(
            "/api/admin/voice/preview",
            headers=headers,
            json={**body, "phrase": "x" * 501},
        )
        statuses = []
        for _ in range(30):
            statuses.append(
                (
                    await http.post(
                        "/api/admin/voice/preview", headers=headers, json=body
                    )
                ).status_code
            )

    async with _client(app) as partner_http:
        # A separate browser session under a non-administrator identity.
        partner_headers = await _mutation_headers(
            partner_http, identity="partner@example.com"
        )
        partner = await partner_http.post(
            "/api/admin/voice/preview", headers=partner_headers, json=body
        )

    assert anonymous.status_code == 403
    assert partner.status_code == 403
    assert too_long.status_code == 400 and too_long.json()["error"] == "invalid_phrase"
    assert 429 in statuses, "the preview endpoint is not rate limited"


def test_preview_is_bounded_authorized_and_rate_limited(tmp_path, monkeypatch) -> None:
    asyncio.run(_preview_is_bounded_authorized_and_rate_limited(tmp_path, monkeypatch))


async def _preview_never_returns_credential_material(tmp_path, monkeypatch):
    app, _service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        preview = await http.post(
            "/api/admin/voice/preview",
            headers=headers,
            json={
                "name": "preview",
                "provider": "openai_realtime",
                "model": "gpt-realtime-2.1-mini",
                "voice": "cedar",
                "speed": 1.0,
                "phrase": "Hello.",
            },
        )

    assert SENTINEL.encode() not in preview.content
    assert SENTINEL not in json.dumps(dict(preview.headers))


def test_preview_never_returns_credential_material(tmp_path, monkeypatch) -> None:
    asyncio.run(_preview_never_returns_credential_material(tmp_path, monkeypatch))


# ------------------------ effective runtime reporting -----------------------


async def _saved_and_effective_are_reported_separately(tmp_path, monkeypatch):
    app, service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        applied = (await _set_voice(http, headers, voice="coral")).json()
        state = (await http.get("/api/admin/ai/settings", headers=headers)).json()

    assert applied["saved"]["speech"]["voice"] == "coral"
    assert applied["effective"]["speech"]["voice"] == "coral"
    assert applied["in_sync"]["speech"] is True
    assert applied["activation_error"] is None
    assert state["effective"]["speech"]["voice"] == "coral"
    assert service.ai.effective.speech.voice == "coral"


def test_saved_and_effective_are_reported_separately(tmp_path, monkeypatch) -> None:
    asyncio.run(_saved_and_effective_are_reported_separately(tmp_path, monkeypatch))


async def _a_failed_runtime_reload_is_not_reported_as_active(tmp_path, monkeypatch):
    app, service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="cedar")
        previous = service.cloud_tts

        def broken(_api_key):
            raise RuntimeError("speech provider could not be initialized")

        service.ai._speech_factory = broken
        applied = (await _set_voice(http, headers, voice="verse")).json()

    assert applied["saved"]["speech"]["voice"] == "verse"
    # The runtime never took the new value, and nothing claims it did.
    assert applied["effective"]["speech"]["voice"] == "cedar"
    assert applied["in_sync"]["speech"] is False
    assert applied["activation_error"]["code"] == "activation_failed"
    assert service.cloud_tts is previous


def test_a_failed_runtime_reload_is_not_reported_as_active(tmp_path, monkeypatch) -> None:
    asyncio.run(_a_failed_runtime_reload_is_not_reported_as_active(tmp_path, monkeypatch))


async def _settings_persist_across_a_service_restart(tmp_path, monkeypatch):
    app, service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="marin")
        await http.post(
            "/api/admin/ai/chat",
            headers=headers,
            json={
                "provider": "openai",
                "model": "gpt-5.6-sol",
                "reasoning_effort": "xhigh",
                "verbosity": "low",
            },
        )
    settings = service.settings
    restarted = BetaAssistantService(
        settings,
        service.vocabulary,
        general_reasoner=NoCloud(),
        state_dir=service.state_dir,
    )

    assert restarted.ai.effective.speech.voice == "marin"
    assert restarted.ai.effective.speech.speed is None
    assert restarted.ai.effective.chat.model == "gpt-5.6-sol"
    assert restarted.ai.effective.chat.reasoning_effort == "xhigh"
    assert restarted.ai.effective.chat.verbosity == "low"


def test_settings_persist_across_a_service_restart(tmp_path, monkeypatch) -> None:
    asyncio.run(_settings_persist_across_a_service_restart(tmp_path, monkeypatch))


async def _switching_provider_excludes_stale_incompatible_parameters(tmp_path, monkeypatch):
    app, service, _recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="cedar", instructions="Brisk.")
        local = (
            await http.post(
                "/api/admin/ai/tts",
                headers=headers,
                json={
                    "provider": "local",
                    "model": "local-piper",
                    "voice": "kathleen",
                    "speed": 1.0,
                },
            )
        ).json()
        back = (await _set_voice(http, headers, voice="cedar", instructions="Brisk.")).json()

    # The local profile carries no style; it is not inherited from OpenAI.
    assert local["effective"]["speech"] == {
        "provider": "local",
        "model": "local-piper",
        "voice": "kathleen",
        "speed": 1.0,
        "instructions": None,
        "audio_format": None,
    }
    # Switching back restores the OpenAI profile exactly.
    assert back["effective"]["speech"]["voice"] == "cedar"
    assert back["effective"]["speech"]["instructions"] == "Brisk."
    assert back["profiles"]["speech"]["local"]["voice"] == "kathleen"
    assert back["profiles"]["speech"]["openai_realtime"]["voice"] == "cedar"
    assert service.ai.effective.speech.provider == "openai_realtime"


def test_switching_provider_excludes_stale_incompatible_parameters(tmp_path, monkeypatch) -> None:
    asyncio.run(_switching_provider_excludes_stale_incompatible_parameters(tmp_path, monkeypatch))


# ------------------- per-model voice catalogs, end to end -------------------


async def _every_catalogued_voice_reaches_synthesis_for_its_model(tmp_path, monkeypatch):
    """Walk the catalog the browser is given and synthesize with each voice.

    This closes the gap a unit test cannot: a voice can be listed, accepted by
    validation, and still never reach the provider. Here each one is asserted
    in the request body that left the process.

    Thirty-one iterations would exhaust the request, administrator, and
    speech rate limits several times over, and those are controls worth
    keeping rather than widening for a test. The catalog is therefore fetched
    over HTTP - it is the exact document the browser builds its dropdowns
    from - and the loop then calls the same three service methods the
    endpoints call once they have admitted a request. The HTTP authorization,
    rate limiting, and error mapping around them are covered by the other
    tests in this file, including the two-voice end-to-end proof above.
    """

    app, service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        catalog = (await http.get("/api/admin/ai/catalog", headers=headers)).json()
        openai = next(
            item
            for item in catalog["speech_providers"]
            if item["id"] == "openai_realtime"
        )
        observed: dict[str, list[str]] = {}
        for model in openai["speech_models"]:
            observed[model["id"]] = []
            for voice in model["voices"]:
                session = service.sessions.get(http.cookies["butters_session"])
                applied = service.apply_speech_settings(
                    session,
                    {
                        "provider": "openai_realtime",
                        "model": model["id"],
                        "voice": voice["id"],
                    },
                )
                assert applied["in_sync"]["speech"] is True
                answer = service.handle_text(
                    session, "what is the humidity in box three"
                )
                service.synthesize_trace_response(session, answer.trace_id)
                assert recorder.last["model"] == model["id"]
                observed[model["id"]].append(str(recorder.last["voice"]))

    assert observed["gpt-realtime-2.1-mini"] == [
        "alloy",
        "ash",
        "ballad",
        "cedar",
        "coral",
        "echo",
        "marin",
        "sage",
        "shimmer",
        "verse",
    ]
    assert set(observed) == {"gpt-realtime-2.1-mini"}


def test_every_catalogued_voice_reaches_synthesis_for_its_model(tmp_path, monkeypatch) -> None:
    asyncio.run(
        _every_catalogued_voice_reaches_synthesis_for_its_model(tmp_path, monkeypatch)
    )


async def _all_traditional_tts_models_are_selectable_over_http(tmp_path, monkeypatch):
    app, service, recorder = _application(tmp_path, monkeypatch)
    async with _client(app) as http:
        headers = await _mutation_headers(http)
        await _set_voice(http, headers, voice="sage")
        for model in ("tts-1", "tts-1-hd", "gpt-4o-mini-tts"):
            accepted = await http.post(
                "/api/admin/ai/tts",
                headers=headers,
                json={"provider": "openai", "model": model, "voice": "alloy"},
            )
            assert accepted.status_code == 200, model
            assert accepted.json()["effective"]["speech"]["model"] == model
        await _set_voice(http, headers, voice="sage")
        await _spoken_chat_answer(http, headers)

    # Refused changes never replace the last accepted effective profile.
    assert service.ai.effective.speech.voice == "sage"
    assert recorder.last["voice"] == "sage"
    assert recorder.last["model"] == "gpt-realtime-2.1-mini"


def test_all_traditional_tts_models_are_selectable_over_http(tmp_path, monkeypatch) -> None:
    asyncio.run(_all_traditional_tts_models_are_selectable_over_http(tmp_path, monkeypatch))
