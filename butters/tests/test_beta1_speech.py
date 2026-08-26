from __future__ import annotations

import io
import json
import wave
from dataclasses import replace
from pathlib import Path

from butters.assistant_config import load_assistant_settings
from butters.tts.model import SynthesizedSpeech
from butters.web.speech import (
    LocalTTSProvider,
    OpenAISTTProvider,
    OpenAITTSProvider,
    VoicePreset,
    VoicePresetStore,
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
        target.setframerate(16000)
        target.writeframes(b"\0\0" * 1600)
    return output.getvalue()


def test_voice_presets_persist_only_non_secret_configuration(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = VoicePresetStore(path)
    saved = store.save(
        VoicePreset("calm", "openai", "gpt-4o-mini-tts", "cedar", 0.95, "Calm and concise."),
        make_default=True,
    )

    restarted = VoicePresetStore(path)

    assert saved.is_default
    assert restarted.list() == (saved,)
    assert b"OPENAI_API_KEY" not in path.read_bytes()


def test_local_tts_reloads_engine_when_preview_speed_changes() -> None:
    speeds: list[float] = []

    class Engine:
        def synthesize(self, _text: str) -> SynthesizedSpeech:
            return SynthesizedSpeech(b"\0\0" * 160, 16000, 0.01, 0.01)

        def close(self) -> None:
            pass

    provider = LocalTTSProvider(lambda speed: speeds.append(speed) or Engine())
    provider.synthesize("one", VoicePreset("one", "local", "local-piper", "kathleen", 1.0, ""))
    provider.synthesize("two", VoicePreset("two", "local", "local-piper", "kathleen", 0.8, ""))

    assert speeds == [1.0, 0.8]


def test_openai_tts_uses_reviewed_parameters_and_never_returns_key() -> None:
    base = load_assistant_settings()
    settings = replace(
        base,
        providers=replace(
            base.providers,
            allow_paid_tts=True,
            cloud_tts_price_per_million_characters_usd=15.0,
        ),
    )
    observed = {}

    def opener(request, **_kwargs):
        observed["url"] = request.full_url
        observed["body"] = json.loads(request.data)
        observed["authorization"] = request.headers["Authorization"]
        return Response(_wav())

    result = OpenAITTSProvider(settings, api_key="fake-key", opener=opener).synthesize(
        "Hello Butters",
        VoicePreset("test", "openai", "gpt-4o-mini-tts", "cedar", 1.1, "Warm."),
    )

    assert observed["url"].endswith("/v1/audio/speech")
    assert observed["body"] == {
        "model": "gpt-4o-mini-tts",
        "input": "Hello Butters",
        "voice": "cedar",
        "instructions": "Warm.",
        "response_format": "wav",
        "speed": 1.1,
    }
    assert result.provider == "openai" and result.estimated_cost_usd
    assert "fake-key" not in repr(result)


def test_openai_stt_is_bounded_multipart_and_reports_actual_usage() -> None:
    base = load_assistant_settings()
    settings = replace(
        base,
        providers=replace(
            base.providers,
            allow_paid_stt=True,
            cloud_stt_price_per_minute_usd=0.006,
        ),
    )
    observed = {}

    def opener(request, **_kwargs):
        observed["url"] = request.full_url
        observed["content_type"] = request.headers["Content-type"]
        observed["body"] = request.data
        return Response(json.dumps({"text": "box three humidity", "usage": {"input_tokens": 11, "output_tokens": 3}}).encode())

    result = OpenAISTTProvider(settings, api_key="fake-key", opener=opener).transcribe_wav(
        _wav(), duration_seconds=0.1
    )

    assert observed["url"].endswith("/v1/audio/transcriptions")
    assert b'gpt-4o-mini-transcribe' in observed["body"]
    assert b'utterance.wav' in observed["body"]
    assert result.text == "box three humidity"
    assert result.input_tokens == 11 and result.output_tokens == 3
