from __future__ import annotations

import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest
from butters.ai.capabilities import CapabilityError, build_registry
from butters.ai.store import AISettingsStore
from butters.assistant_config import SpeechProviderSettings, load_assistant_settings
from butters.web.speech import (
    OpenAISTTProvider,
    OpenAITTSProvider,
    SpeechProviderError,
    VoicePreset,
    VoicePresetStore,
)


def _paid_settings():
    base = load_assistant_settings()
    return replace(
        base,
        providers=replace(
            base.providers,
            allow_paid_stt=True,
            allow_paid_tts=True,
            cloud_stt_price_per_minute_usd=0.0045,
        ),
    )


def test_legacy_static_provider_configuration_migrates_to_supported_models() -> None:
    migrated = SpeechProviderSettings(
        cloud_stt_model="gpt-4o-mini-transcribe",
        cloud_tts_model="gpt-4o-mini-tts",
        cloud_tts_voice="cedar",
        cloud_tts_speed=1.25,
        cloud_stt_price_per_minute_usd=0.003,
    ).validated()

    assert migrated.cloud_stt_model == "gpt-transcribe"
    assert migrated.cloud_stt_price_per_minute_usd == 0.0045
    assert migrated.cloud_tts_model == "gpt-realtime-2.1-mini"
    assert migrated.cloud_tts_voice == "cedar"
    assert migrated.cloud_tts_speed == 1.0


def test_saved_admin_profile_is_migrated_durably(tmp_path: Path) -> None:
    registry = build_registry(load_assistant_settings())
    store = AISettingsStore(tmp_path / "state.sqlite3")
    store._write(
        "speech",
        "openai",
        {
            "provider": "openai",
            "model": "gpt-4o-mini-tts",
            "voice": "cedar",
            "speed": 1.2,
            "instructions": "Warm and concise.",
            "audio_format": "wav",
        },
    )

    migrated = store.speech(registry, "local")

    assert migrated.model == "gpt-realtime-2.1-mini"
    assert migrated.voice == "cedar"
    assert migrated.speed is None
    assert migrated.instructions == "Warm and concise."
    assert store.profiles("speech")["openai"] == migrated.as_dict()


def test_legacy_preset_migrates_and_falls_back_from_removed_voice(tmp_path: Path) -> None:
    store = VoicePresetStore(tmp_path / "state.sqlite3")
    store.save(
        VoicePreset("legacy", "openai", "gpt-4o-mini-tts", "onyx", 1.2, "Calm."),
        make_default=True,
    )

    migrated = store.list()[0]
    restarted = VoicePresetStore(tmp_path / "state.sqlite3").list()[0]

    assert migrated == restarted
    assert migrated.model == "gpt-realtime-2.1-mini"
    assert migrated.voice == "alloy"
    assert migrated.speed == 1.0
    assert migrated.instructions == "Calm."


@pytest.mark.parametrize("model", ("tts-1", "tts-1-hd", "gpt-4o-mini-tts"))
def test_retired_tts_models_are_distinguished_from_unknown_models(model: str) -> None:
    registry = build_registry(load_assistant_settings())
    with pytest.raises(CapabilityError) as retired:
        registry.speech_model("openai", model)
    with pytest.raises(CapabilityError) as unknown:
        registry.speech_model("openai", "made-up-speech")

    assert retired.value.code == "model_retired"
    assert unknown.value.code == "model_denied"


def test_retired_stt_model_is_refused_before_an_http_request() -> None:
    settings = _paid_settings()
    settings = replace(
        settings,
        providers=replace(settings.providers, cloud_stt_model="whisper-1"),
    )
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP must not be called")

    with pytest.raises(SpeechProviderError) as retired:
        OpenAISTTProvider(settings, api_key="test-key", opener=opener).transcribe_wav(
            b"wav", duration_seconds=1.0
        )

    assert retired.value.code == "model_retired"
    assert called is False


def test_stt_authentication_error_has_a_specific_code() -> None:
    settings = _paid_settings()

    def opener(request, **_kwargs):
        raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

    with pytest.raises(SpeechProviderError) as denied:
        OpenAISTTProvider(settings, api_key="test-key", opener=opener).transcribe_wav(
            b"wav", duration_seconds=1.0
        )

    assert denied.value.code == "authentication_failed"


def test_realtime_tts_timeout_has_a_specific_code() -> None:
    settings = _paid_settings()

    def connector(*_args, **_kwargs):
        raise TimeoutError("connect deadline")

    with pytest.raises(SpeechProviderError) as timed_out:
        OpenAITTSProvider(settings, api_key="test-key", connector=connector).synthesize(
            "hello",
            VoicePreset(
                "test",
                "openai",
                "gpt-realtime-2.1-mini",
                "cedar",
                1.0,
                "",
            ),
        )

    assert timed_out.value.code == "timeout"


def test_realtime_error_event_maps_authentication_without_leaking_detail() -> None:
    settings = _paid_settings()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def recv(self, *, timeout: float):
            return json.dumps(
                {
                    "type": "error",
                    "error": {
                        "code": "invalid_api_key",
                        "message": "bad key sk-secret-material",
                    },
                }
            )

    with pytest.raises(SpeechProviderError) as denied:
        OpenAITTSProvider(
            settings, api_key="test-key", connector=lambda *_args, **_kwargs: Connection()
        ).synthesize(
            "hello",
            VoicePreset(
                "test",
                "openai",
                "gpt-realtime-2.1-mini",
                "cedar",
                1.0,
                "",
            ),
        )

    assert denied.value.code == "authentication_failed"
    assert "sk-secret-material" not in str(denied.value)


def test_realtime_completed_response_without_audio_is_rejected() -> None:
    settings = _paid_settings()

    class Connection:
        def __init__(self) -> None:
            self.events = iter(
                (
                    {"type": "session.created"},
                    {"type": "session.updated"},
                    {"type": "response.done", "response": {"status": "completed"}},
                )
            )

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def send(self, _message: str) -> None:
            return None

        def recv(self, *, timeout: float) -> str:
            assert timeout > 0
            return json.dumps(next(self.events))

    provider = OpenAITTSProvider(
        settings, api_key="test-key", connector=lambda *_args, **_kwargs: Connection()
    )
    with pytest.raises(SpeechProviderError) as malformed:
        provider.synthesize(
            "hello",
            VoicePreset(
                "test",
                "openai",
                "gpt-realtime-2.1-mini",
                "cedar",
                1.0,
                "",
            ),
        )

    assert malformed.value.code == "malformed_audio"
