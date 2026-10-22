"""Regression coverage for the retained traditional OpenAI Speech API path."""

from __future__ import annotations

import io
import json
import wave
from dataclasses import replace

import pytest
from butters.ai.capabilities import CapabilityError, build_registry
from butters.ai.model import validate_speech
from butters.assistant_config import load_assistant_settings
from butters.pricing import (
    SPEECH_PRICING,
    CharacterPricing,
    CostBasis,
    SpeechTokenPricing,
    speech_cost,
)
from butters.web.speech import OpenAITTSProvider, SpeechProviderError, VoicePreset

EXPRESSIVE_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
CLASSIC_VOICES = (
    "alloy",
    "ash",
    "coral",
    "echo",
    "fable",
    "nova",
    "onyx",
    "sage",
    "shimmer",
)


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b"\0\0" * 2400)
    return output.getvalue()


@pytest.fixture()
def registry():
    return build_registry(load_assistant_settings())


def test_traditional_and_realtime_are_distinct_selectable_providers(registry) -> None:
    traditional = registry.provider("openai")
    realtime = registry.provider("openai_realtime")

    assert {item.id for item in traditional.speech_models} == {
        "gpt-4o-mini-tts",
        "gpt-4o-mini-tts-2025-12-15",
        "tts-1",
        "tts-1-hd",
    }
    assert {item.id for item in realtime.speech_models} == {
        "gpt-realtime-2.1-mini"
    }


@pytest.mark.parametrize(
    ("model", "voices"),
    (
        ("gpt-4o-mini-tts", EXPRESSIVE_VOICES),
        ("gpt-4o-mini-tts-2025-12-15", EXPRESSIVE_VOICES),
        ("tts-1", CLASSIC_VOICES),
        ("tts-1-hd", CLASSIC_VOICES),
    ),
)
def test_traditional_model_voice_catalogs_are_exact(registry, model, voices) -> None:
    capability = registry.speech_model("openai", model)

    assert capability.voice_ids() == voices
    for voice in voices:
        assert validate_speech(
            registry,
            {"provider": "openai", "model": model, "voice": voice},
        ).voice == voice


@pytest.mark.parametrize("model", ("tts-1", "tts-1-hd"))
@pytest.mark.parametrize("voice", ("ballad", "cedar", "marin", "verse"))
def test_classic_models_reject_expressive_only_voices(registry, model, voice) -> None:
    with pytest.raises(CapabilityError) as denied:
        validate_speech(
            registry,
            {"provider": "openai", "model": model, "voice": voice},
        )
    assert denied.value.code == "invalid_voice"


@pytest.mark.parametrize(
    ("model", "voice", "instructions", "expected_instructions"),
    (
        ("gpt-4o-mini-tts", "cedar", "Warm.", "Warm."),
        ("gpt-4o-mini-tts-2025-12-15", "marin", "Calm.", "Calm."),
        ("tts-1", "alloy", "", None),
        ("tts-1-hd", "onyx", "", None),
    ),
)
def test_every_traditional_model_reaches_audio_speech_endpoint(
    model, voice, instructions, expected_instructions
) -> None:
    base = load_assistant_settings()
    settings = replace(
        base, providers=replace(base.providers, allow_paid_tts=True)
    )
    observed = {}

    def opener(request, **_kwargs):
        observed["url"] = request.full_url
        observed["body"] = json.loads(request.data)
        return Response(_wav())

    result = OpenAITTSProvider(settings, api_key="test-key", opener=opener).synthesize(
        "Compatibility check",
        VoicePreset("legacy", "openai", model, voice, 1.25, instructions),
    )

    assert observed["url"].endswith("/v1/audio/speech")
    assert observed["body"]["model"] == model
    assert observed["body"]["speed"] == 1.25
    assert observed["body"].get("instructions") == expected_instructions
    assert result.provider == "openai"
    assert result.model == model


@pytest.mark.parametrize(("model", "rate"), (("tts-1", 15.0), ("tts-1-hd", 30.0)))
def test_classic_model_cost_is_measured_from_submitted_characters(model, rate) -> None:
    cost = speech_cost(model, characters=200)

    assert isinstance(SPEECH_PRICING[model], CharacterPricing)
    assert cost.amount_usd == pytest.approx(200 * rate / 1_000_000)
    assert cost.basis is CostBasis.INPUT_MEASURED


@pytest.mark.parametrize(
    "model", ("gpt-4o-mini-tts", "gpt-4o-mini-tts-2025-12-15")
)
def test_gpt_4o_speech_cost_is_a_fail_closed_token_ceiling(model) -> None:
    assert isinstance(SPEECH_PRICING[model], SpeechTokenPricing)
    assert speech_cost(model, characters=200).basis is CostBasis.ESTIMATED_UPPER_BOUND


def test_paid_gate_blocks_traditional_tts_before_any_http_request() -> None:
    settings = load_assistant_settings()
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("paid-disabled synthesis must not make a request")

    provider = OpenAITTSProvider(settings, api_key="test-key", opener=opener)
    with pytest.raises(SpeechProviderError) as denied:
        provider.synthesize(
            "No request",
            VoicePreset(
                "legacy", "openai", "gpt-4o-mini-tts", "cedar", 1.0, ""
            ),
        )

    assert denied.value.code == "paid_tts_disabled"
    assert called is False
