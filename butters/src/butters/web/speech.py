"""Local/cloud STT and TTS provider boundaries plus non-secret voice presets."""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from butters.ai.audio_models import (
    DEPRECATED_OPENAI_STT_MODELS,
    OPENAI_REALTIME_TTS_MODEL,
    OPENAI_STT_MODEL,
    OPENAI_STT_SHUTDOWN_DATE,
)
from butters.ai.capabilities import CapabilityError, CapabilityRegistry, build_registry
from butters.ai.model import SpeechSettings
from butters.assistant_config import AssistantSettings
from butters.audio.wav import WavFormatError, wav_duration_seconds
from butters.pricing import SpeechCost, speech_cost, speech_pricing
from butters.tts.model import SynthesizedSpeech, TextToSpeechEngine, TTSError


class SpeechProviderError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SpeechResult:
    audio_wav: bytes
    provider: str
    model: str
    voice: str
    generation_seconds: float
    # None when the container is valid but its duration cannot be derived.
    audio_seconds: float | None
    estimated_cost_usd: float | None = None
    # How the cost above was obtained. None for the local engine, which is free.
    cost: SpeechCost | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    provider: str
    model: str
    elapsed_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class VoicePreset:
    name: str
    provider: str
    model: str
    voice: str
    speed: float
    instructions: str
    is_default: bool = False


class VoicePresetStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS voice_presets (
                    name TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    voice TEXT NOT NULL,
                    speed REAL NOT NULL,
                    instructions TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def list(self) -> tuple[VoicePreset, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT name, provider, model, voice, speed, instructions, is_default "
                "FROM voice_presets ORDER BY is_default DESC, name"
            ).fetchall()
            presets: list[VoicePreset] = []
            for row in rows:
                original = VoicePreset(*row[:6], bool(row[6]))
                migrated = (
                    VoicePreset(
                        original.name,
                        "openai_realtime",
                        original.model,
                        original.voice,
                        original.speed,
                        original.instructions,
                        original.is_default,
                    )
                    if original.provider == "openai"
                    and original.model == OPENAI_REALTIME_TTS_MODEL
                    else original
                )
                presets.append(migrated)
                if migrated != original:
                    connection.execute(
                        """UPDATE voice_presets SET provider=?,
                        updated_at=CURRENT_TIMESTAMP WHERE name=?""",
                        (migrated.provider, migrated.name),
                    )
        return tuple(presets)

    def save(self, preset: VoicePreset, *, make_default: bool = False) -> VoicePreset:
        _validate_preset(preset)
        with self._lock, self._connect() as connection:
            if make_default:
                connection.execute("UPDATE voice_presets SET is_default=0")
            connection.execute(
                """INSERT INTO voice_presets
                (name, provider, model, voice, speed, instructions, is_default)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(name) DO UPDATE SET provider=excluded.provider,
                model=excluded.model, voice=excluded.voice, speed=excluded.speed,
                instructions=excluded.instructions, is_default=excluded.is_default,
                updated_at=CURRENT_TIMESTAMP""",
                (
                    preset.name,
                    preset.provider,
                    preset.model,
                    preset.voice,
                    preset.speed,
                    preset.instructions,
                    int(make_default or preset.is_default),
                ),
            )
        return VoicePreset(
            preset.name,
            preset.provider,
            preset.model,
            preset.voice,
            preset.speed,
            preset.instructions,
            make_default or preset.is_default,
        )

    def default(self, settings: AssistantSettings) -> VoicePreset:
        presets = self.list()
        selected = next((item for item in presets if item.is_default), None)
        if selected is not None:
            return selected
        provider = settings.providers.tts_default
        if provider == "openai_realtime":
            return VoicePreset(
                "configured-default",
                provider,
                OPENAI_REALTIME_TTS_MODEL,
                "cedar",
                1.0,
                settings.providers.cloud_tts_instructions,
                True,
            )
        return VoicePreset(
            "configured-default",
            provider,
            "local-piper" if provider == "local" else settings.providers.cloud_tts_model,
            "kathleen" if provider == "local" else settings.providers.cloud_tts_voice,
            settings.tts.speed if provider == "local" else settings.providers.cloud_tts_speed,
            "" if provider == "local" else settings.providers.cloud_tts_instructions,
            True,
        )

    def as_dicts(self) -> list[dict[str, object]]:
        return [asdict(item) for item in self.list()]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection


class LocalTTSProvider:
    def __init__(self, engine_factory: callable) -> None:
        self.engine_factory = engine_factory
        self._engine: TextToSpeechEngine | None = None
        self._speed: float | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return True

    def synthesize(self, text: str, preset: VoicePreset) -> SpeechResult:
        if preset.provider != "local":
            raise SpeechProviderError("provider_mismatch", "local TTS preset is required")
        with self._lock:
            try:
                if self._engine is not None and self._speed != preset.speed:
                    self._engine.close()
                    self._engine = None
                if self._engine is None:
                    self._engine = self.engine_factory(preset.speed)
                    self._speed = preset.speed
                speech = self._engine.synthesize(text)
            except (TTSError, OSError, RuntimeError, ValueError) as exc:
                raise SpeechProviderError("local_tts_unavailable", "local speech synthesis is unavailable") from exc
        return SpeechResult(
            _speech_to_wav(speech),
            "local",
            "local-piper",
            preset.voice,
            speech.generation_seconds,
            speech.audio_seconds,
            0.0,
        )

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
                self._speed = None


class OpenAITTSProvider:
    """Bounded adapter for traditional Speech API and Realtime TTS.

    The Admin catalog exposes these as distinct providers. They share the
    credential and budget boundary, but never share a request path: ``openai``
    posts to ``/v1/audio/speech`` while ``openai_realtime`` uses a short-lived
    WebSocket session.
    """

    def __init__(
        self,
        settings: AssistantSettings,
        *,
        api_key: str | None = None,
        opener: callable = urllib.request.urlopen,
        connector: callable | None = None,
        registry: CapabilityRegistry | None = None,
    ) -> None:
        self.settings = settings
        self._api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self._opener = opener
        self._connector = connector or _default_websocket_connect
        # The registry, not a template or a conditional here, decides which
        # voices exist and whether this model consumes speaking instructions.
        self.registry = registry or build_registry(settings)

    @property
    def available(self) -> bool:
        # Pricing is now per model and server-authoritative, so availability
        # no longer depends on an operator having set one free-floating
        # character rate that was wrong for gpt-4o-mini-tts anyway.
        return bool(self._api_key and self.settings.providers.allow_paid_tts)

    def synthesize(self, text: str, preset: VoicePreset) -> SpeechResult:
        if not self._api_key:
            raise SpeechProviderError("missing_api_key", "OpenAI credential is not configured")
        if not self.settings.providers.allow_paid_tts:
            raise SpeechProviderError("paid_tts_disabled", "paid TTS is disabled")
        if speech_pricing(preset.model) is None:
            raise SpeechProviderError(
                "pricing_unknown", f"{preset.model} has no reviewed speech pricing"
            )
        if preset.provider not in {"openai", "openai_realtime"}:
            raise SpeechProviderError("provider_mismatch", "OpenAI TTS preset is required")
        try:
            capability = self.registry.speech_model(preset.provider, preset.model)
        except CapabilityError as exc:
            raise SpeechProviderError(exc.code, str(exc)) from exc
        if preset.voice not in capability.voice_ids():
            raise SpeechProviderError(
                "invalid_voice", f"{preset.voice} is not a voice of {capability.id}"
            )
        value = " ".join(text.split())
        if not value or len(value) > 2000:
            raise SpeechProviderError("invalid_text", "TTS text must be 1 to 2000 characters")
        if preset.provider == "openai":
            return self._synthesize_speech_api(value, preset, capability)
        return self._synthesize_realtime_result(value, preset)

    def _synthesize_speech_api(
        self, value: str, preset: VoicePreset, capability
    ) -> SpeechResult:
        body: dict[str, object] = {
            "model": preset.model,
            "input": value,
            "voice": preset.voice,
            "response_format": "wav",
        }
        if capability.supports_instructions and preset.instructions:
            body["instructions"] = preset.instructions[:1000]
        if capability.supports_speed:
            body["speed"] = preset.speed
        request = urllib.request.Request(
            f"{self.settings.cloud.base_url}/v1/audio/speech",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Butters-Beta1/1.0",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(
                request, timeout=self.settings.cloud.timeout_seconds
            ) as response:
                audio = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise _status_error("TTS", exc.code) from exc
        except TimeoutError as exc:
            raise SpeechProviderError("timeout", "OpenAI TTS timed out") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechProviderError("unavailable", "OpenAI TTS is unavailable") from exc
        elapsed = time.perf_counter() - started
        if not audio:
            raise SpeechProviderError("malformed_audio", "OpenAI TTS returned no audio")
        if len(audio) > 8 * 1024 * 1024:
            raise SpeechProviderError("response_too_large", "TTS audio exceeded the byte limit")
        duration = _wav_duration(audio)
        cost = speech_cost(preset.model, characters=len(value))
        return SpeechResult(
            audio,
            "openai",
            preset.model,
            preset.voice,
            elapsed,
            duration,
            cost.amount_usd,
            cost,
        )

    def _synthesize_realtime_result(
        self, value: str, preset: VoicePreset
    ) -> SpeechResult:
        body: dict[str, object] = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": preset.model,
                "output_modalities": ["audio"],
                "instructions": _tts_instructions(preset.instructions),
                "audio": {
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": preset.voice,
                    }
                },
            },
        }
        started = time.perf_counter()
        try:
            raw_pcm, usage = self._synthesize_realtime(body, value)
        except SpeechProviderError:
            raise
        except TimeoutError as exc:
            raise SpeechProviderError("timeout", "OpenAI TTS timed out") from exc
        except Exception as exc:  # websocket implementations expose varied errors
            status = _exception_status(exc)
            if status is not None:
                raise _status_error("TTS", status) from exc
            raise SpeechProviderError("unavailable", "OpenAI TTS is unavailable") from exc
        elapsed = time.perf_counter() - started
        if not raw_pcm:
            raise SpeechProviderError("malformed_audio", "OpenAI TTS returned no audio")
        if len(raw_pcm) > 8 * 1024 * 1024:
            raise SpeechProviderError("response_too_large", "TTS audio exceeded the byte limit")
        if len(raw_pcm) % 2:
            raise SpeechProviderError(
                "malformed_audio", "OpenAI TTS returned an incomplete PCM sample"
            )
        audio = _pcm16_wav(raw_pcm, sample_rate=24000)
        duration = len(raw_pcm) / (24000 * 2)
        input_tokens, output_tokens = _speech_usage(usage)
        cost = speech_cost(
            preset.model,
            characters=len(value),
            reported_text_input_tokens=input_tokens,
            reported_audio_output_tokens=output_tokens,
        )
        return SpeechResult(
            audio,
            "openai_realtime",
            preset.model,
            preset.voice,
            elapsed,
            duration,
            cost.amount_usd,
            cost,
        )

    def _synthesize_realtime(
        self, session_update: dict[str, object], text: str
    ) -> tuple[bytes, dict[str, object] | None]:
        timeout = self.settings.cloud.timeout_seconds
        deadline = time.monotonic() + timeout
        url = (
            self.settings.cloud.base_url.replace("https://", "wss://", 1)
            + f"/v1/realtime?model={OPENAI_REALTIME_TTS_MODEL}"
        )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "Butters-Beta1/1.0",
        }
        with self._connector(
            url,
            additional_headers=headers,
            open_timeout=timeout,
            close_timeout=min(timeout, 5.0),
            max_size=2 * 1024 * 1024,
        ) as connection:
            _wait_for_event(connection, deadline, "session.created")
            connection.send(json.dumps(session_update, separators=(",", ":")))
            _wait_for_event(connection, deadline, "session.updated")
            connection.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    },
                    separators=(",", ":"),
                )
            )
            connection.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {"output_modalities": ["audio"]},
                    },
                    separators=(",", ":"),
                )
            )
            chunks: list[bytes] = []
            total = 0
            while True:
                event = _receive_event(connection, deadline)
                event_type = event.get("type")
                if event_type == "error":
                    raise _realtime_event_error(event)
                if event_type == "response.output_audio.delta":
                    encoded = event.get("delta")
                    if not isinstance(encoded, str):
                        raise SpeechProviderError(
                            "malformed_response", "OpenAI TTS returned an invalid audio event"
                        )
                    try:
                        chunk = base64.b64decode(encoded, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise SpeechProviderError(
                            "malformed_response", "OpenAI TTS returned invalid base64 audio"
                        ) from exc
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise SpeechProviderError(
                            "response_too_large", "TTS audio exceeded the byte limit"
                        )
                    chunks.append(chunk)
                    continue
                if event_type != "response.done":
                    continue
                response = event.get("response")
                if not isinstance(response, dict) or response.get("status") != "completed":
                    raise SpeechProviderError(
                        "upstream_response_failed", "OpenAI TTS did not complete the response"
                    )
                usage = response.get("usage")
                return b"".join(chunks), usage if isinstance(usage, dict) else None


class OpenAISTTProvider:
    """Optional bounded WAV transcription; browser streaming remains local by default."""

    def __init__(
        self,
        settings: AssistantSettings,
        *,
        api_key: str | None = None,
        opener: callable = urllib.request.urlopen,
    ) -> None:
        self.settings = settings
        self._api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self._opener = opener

    @property
    def available(self) -> bool:
        return bool(
            self._api_key
            and self.settings.providers.allow_paid_stt
            and self.settings.providers.cloud_stt_price_per_minute_usd
        )

    def transcribe_wav(self, audio: bytes, *, duration_seconds: float) -> TranscriptionResult:
        if not self._api_key:
            raise SpeechProviderError("missing_api_key", "OpenAI credential is not configured")
        if not self.settings.providers.allow_paid_stt:
            raise SpeechProviderError("paid_stt_disabled", "paid STT is disabled")
        model = self.settings.providers.cloud_stt_model
        if model in DEPRECATED_OPENAI_STT_MODELS:
            raise SpeechProviderError(
                "model_retired",
                f"{model} is deprecated and shuts down on "
                f"{OPENAI_STT_SHUTDOWN_DATE}; use {OPENAI_STT_MODEL}",
            )
        if model != OPENAI_STT_MODEL:
            raise SpeechProviderError(
                "model_denied", f"{model} is not a supported transcription model"
            )
        price = self.settings.providers.cloud_stt_price_per_minute_usd
        if price is None:
            raise SpeechProviderError("pricing_unknown", "paid STT pricing is not configured")
        if not audio or len(audio) > 8 * 1024 * 1024 or not 0 < duration_seconds <= 120:
            raise SpeechProviderError("audio_limit", "STT audio exceeds its limit")
        boundary = "butters-" + secrets.token_hex(12)
        body = _multipart(
            boundary,
            {"model": model, "response_format": "json"},
            "file",
            "utterance.wav",
            "audio/wav",
            audio,
        )
        request = urllib.request.Request(
            f"{self.settings.cloud.base_url}/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Butters-Beta1/1.0",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with self._opener(request, timeout=self.settings.cloud.timeout_seconds) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise _status_error("STT", exc.code) from exc
        except TimeoutError as exc:
            raise SpeechProviderError("timeout", "OpenAI STT timed out") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SpeechProviderError("unavailable", "OpenAI STT is unavailable") from exc
        if len(raw) > 1024 * 1024:
            raise SpeechProviderError("response_too_large", "STT response exceeded the byte limit")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SpeechProviderError("malformed_response", "STT returned invalid JSON") from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str) or len(text) > 8000:
            raise SpeechProviderError("malformed_response", "STT response text is invalid")
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        return TranscriptionResult(
            text.strip(),
            "openai",
            model,
            time.perf_counter() - started,
            _safe_int(usage.get("input_tokens")),
            _safe_int(usage.get("output_tokens")),
            duration_seconds / 60 * price,
        )


LOCAL_TTS_MODEL = "local-piper"
# Piper is time-scaled by this factor; values outside the reviewed band either
# divide by zero or ask the engine to synthesize minutes of audio per sentence.
LOCAL_SPEED_RANGE = (0.5, 2.0)
CLOUD_SPEED_RANGE = (0.25, 4.0)


def preset_from_speech_settings(
    speech: SpeechSettings, *, name: str = "effective"
) -> VoicePreset:
    """The one translation from stored Admin settings to a synthesis request.

    Butters Chat and Preview both go through this, so a preview cannot honour
    a setting that the spoken chat response would ignore.
    """

    return VoicePreset(
        name,
        speech.provider,
        speech.model,
        speech.voice,
        1.0 if speech.speed is None else speech.speed,
        speech.instructions or "",
        True,
    )


def validate_preset(
    preset: VoicePreset,
    *,
    settings: AssistantSettings | None = None,
    registry: CapabilityRegistry | None = None,
) -> None:
    """Single validation boundary for saved presets and one-off previews."""

    if not preset.name or len(preset.name) > 64 or not all(
        character.isalnum() or character in " _-" for character in preset.name
    ):
        raise SpeechProviderError("invalid_preset", "preset name is invalid")
    if preset.provider not in {"local", "openai", "openai_realtime"}:
        raise SpeechProviderError("invalid_preset", "preset provider is invalid")
    if not preset.model or len(preset.model) > 128 or not preset.voice or len(preset.voice) > 128:
        raise SpeechProviderError("invalid_preset", "preset model or voice is invalid")
    if len(preset.instructions) > 1000:
        raise SpeechProviderError("invalid_preset", "preset parameters exceed their limits")
    speed = preset.speed
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise SpeechProviderError("invalid_preset", "preset speed must be numeric")
    speed = float(speed)
    if not math.isfinite(speed):
        raise SpeechProviderError("invalid_preset", "preset speed must be a finite number")
    low, high = LOCAL_SPEED_RANGE if preset.provider == "local" else CLOUD_SPEED_RANGE
    if not low <= speed <= high:
        raise SpeechProviderError(
            "invalid_preset", f"preset speed must be {low} to {high} for {preset.provider}"
        )
    if preset.provider == "local" and preset.model != LOCAL_TTS_MODEL:
        raise SpeechProviderError("invalid_preset", "local TTS uses the reviewed local model")
    # The capability registry is authoritative for model, voice, and which
    # controls the model accepts. `settings` is accepted only so the existing
    # callers that already hold configuration can derive the same registry.
    catalog = registry
    if catalog is None and settings is not None:
        catalog = build_registry(settings)
    if catalog is None:
        return
    try:
        capability = catalog.speech_model(preset.provider, preset.model)
    except CapabilityError as exc:
        raise SpeechProviderError(exc.code, str(exc)) from exc
    if preset.voice not in capability.voice_ids():
        raise SpeechProviderError(
            "invalid_voice", f"{preset.voice} is not a voice of {capability.id}"
        )
    low, high = capability.speed_range
    if not low <= speed <= high:
        raise SpeechProviderError(
            "invalid_preset", f"{capability.id} accepts a speed of {low} to {high}"
        )
    if preset.instructions and not capability.supports_instructions:
        raise SpeechProviderError(
            "unsupported_parameter",
            f"{capability.id} does not accept speaking instructions",
        )


def _validate_preset(preset: VoicePreset) -> None:
    validate_preset(preset)


def _speech_to_wav(speech: SynthesizedSpeech) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(speech.sample_rate)
        target.writeframes(speech.pcm_s16le)
    return output.getvalue()


def _pcm16_wav(pcm: bytes, *, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm)
    return output.getvalue()


def _default_websocket_connect(url: str, **kwargs):
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise SpeechProviderError(
            "client_unavailable",
            "the WebSocket client required for OpenAI TTS is not installed",
        ) from exc
    return connect(url, **kwargs)


def _receive_event(connection, deadline: float) -> dict[str, object]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Realtime response deadline exceeded")
    message = connection.recv(timeout=remaining)
    if message is None:
        raise SpeechProviderError("unavailable", "OpenAI TTS closed the connection")
    if isinstance(message, bytes):
        try:
            message = message.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpeechProviderError(
                "malformed_response", "OpenAI TTS returned a non-text event"
            ) from exc
    if not isinstance(message, str) or len(message) > 2 * 1024 * 1024:
        raise SpeechProviderError(
            "malformed_response", "OpenAI TTS returned an invalid event"
        )
    try:
        event = json.loads(message)
    except json.JSONDecodeError as exc:
        raise SpeechProviderError(
            "malformed_response", "OpenAI TTS returned invalid JSON"
        ) from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise SpeechProviderError(
            "malformed_response", "OpenAI TTS returned an invalid event"
        )
    return event


def _wait_for_event(connection, deadline: float, expected: str) -> dict[str, object]:
    while True:
        event = _receive_event(connection, deadline)
        if event["type"] == "error":
            raise _realtime_event_error(event)
        if event["type"] == expected:
            return event


def _tts_instructions(style: str) -> str:
    instruction = (
        "Read the user's text aloud exactly as written. Do not answer it, add "
        "commentary, or omit words."
    )
    cleaned = " ".join(style.split())[:1000]
    return instruction if not cleaned else f"{instruction} Speaking style: {cleaned}"


def _speech_usage(
    usage: dict[str, object] | None,
) -> tuple[int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None
    input_details = usage.get("input_token_details")
    output_details = usage.get("output_token_details")
    if not isinstance(input_details, dict) or not isinstance(output_details, dict):
        return None, None
    text_tokens = input_details.get("text_tokens")
    audio_tokens = output_details.get("audio_tokens")
    if (
        isinstance(text_tokens, bool)
        or not isinstance(text_tokens, int)
        or text_tokens < 0
        or isinstance(audio_tokens, bool)
        or not isinstance(audio_tokens, int)
        or audio_tokens < 0
    ):
        return None, None
    return text_tokens, audio_tokens


def _realtime_event_error(event: dict[str, object]) -> SpeechProviderError:
    detail = event.get("error")
    code = str(detail.get("code", "")) if isinstance(detail, dict) else ""
    normalized = code.casefold()
    if "auth" in normalized or "api_key" in normalized:
        return SpeechProviderError(
            "authentication_failed", "OpenAI rejected the configured credential"
        )
    if "rate_limit" in normalized:
        return SpeechProviderError("rate_limited", "OpenAI rate limited TTS")
    if "deprecated" in normalized or "retired" in normalized:
        return SpeechProviderError(
            "model_retired",
            f"the configured OpenAI Realtime TTS model is retired; use {OPENAI_REALTIME_TTS_MODEL}",
        )
    if "model" in normalized and ("not_found" in normalized or "unavailable" in normalized):
        return SpeechProviderError(
            "model_unavailable", "the configured OpenAI TTS model is unavailable"
        )
    if "invalid" in normalized:
        return SpeechProviderError(
            "upstream_configuration", "OpenAI rejected the TTS configuration"
        )
    return SpeechProviderError("upstream_error", "OpenAI TTS returned an error")


def _exception_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _status_error(operation: str, status: int) -> SpeechProviderError:
    if status in {401, 403}:
        return SpeechProviderError(
            "authentication_failed", "OpenAI rejected the configured credential"
        )
    if status == 404:
        return SpeechProviderError(
            "model_unavailable", f"the configured OpenAI {operation} model is unavailable"
        )
    if status == 429:
        return SpeechProviderError("rate_limited", f"OpenAI rate limited {operation}")
    if status in {408, 504}:
        return SpeechProviderError("timeout", f"OpenAI {operation} timed out")
    if status >= 500:
        return SpeechProviderError("unavailable", f"OpenAI {operation} is unavailable")
    if status == 400:
        return SpeechProviderError(
            "upstream_configuration", f"OpenAI rejected the {operation} configuration"
        )
    return SpeechProviderError(
        "upstream_status", f"OpenAI {operation} returned HTTP {status}"
    )


def _wav_duration(audio: bytes) -> float | None:
    """Duration of the audio actually received, or None when unmeasurable.

    A payload that is not a RIFF/WAVE container at all still fails the
    request: a 200 carrying something other than audio is a provider problem,
    not a metadata gap. A well-formed container Butters cannot measure - a
    compressed encoding, say - yields playable audio and an honest "unknown"
    rather than a fabricated number.
    """

    try:
        return wav_duration_seconds(audio)
    except WavFormatError as exc:
        raise SpeechProviderError(
            "malformed_audio", "TTS returned malformed WAV audio"
        ) from exc


def _multipart(
    boundary: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content_type: str,
    content: bytes,
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks)


def _safe_int(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
