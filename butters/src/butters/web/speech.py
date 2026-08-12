"""Local/cloud STT and TTS provider boundaries plus non-secret voice presets."""

from __future__ import annotations

import io
import json
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

from butters.assistant_config import AssistantSettings
from butters.tts.model import SynthesizedSpeech, TTSError, TextToSpeechEngine


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
    audio_seconds: float
    estimated_cost_usd: float | None = None


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
        return tuple(VoicePreset(*row[:6], bool(row[6])) for row in rows)

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
    """Optional OpenAI /v1/audio/speech adapter, disabled and unpriced by default."""

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
            and self.settings.providers.allow_paid_tts
            and self.settings.providers.cloud_tts_price_per_million_characters_usd
        )

    def synthesize(self, text: str, preset: VoicePreset) -> SpeechResult:
        if not self._api_key:
            raise SpeechProviderError("missing_api_key", "OpenAI credential is not configured")
        if not self.settings.providers.allow_paid_tts:
            raise SpeechProviderError("paid_tts_disabled", "paid TTS is disabled")
        price = self.settings.providers.cloud_tts_price_per_million_characters_usd
        if price is None:
            raise SpeechProviderError("pricing_unknown", "paid TTS pricing is not configured")
        if preset.model != self.settings.providers.cloud_tts_model:
            raise SpeechProviderError("model_denied", "TTS model is not configured")
        value = " ".join(text.split())
        if not value or len(value) > 2000:
            raise SpeechProviderError("invalid_text", "TTS text must be 1 to 2000 characters")
        body = {
            "model": preset.model,
            "input": value,
            "voice": preset.voice,
            "instructions": preset.instructions[:1000],
            "response_format": "wav",
            "speed": preset.speed,
        }
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
            with self._opener(request, timeout=self.settings.cloud.timeout_seconds) as response:
                audio = response.read(8 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raise SpeechProviderError("upstream_status", f"OpenAI returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SpeechProviderError("unavailable", "OpenAI TTS is unavailable") from exc
        elapsed = time.perf_counter() - started
        if not audio or len(audio) > 8 * 1024 * 1024:
            raise SpeechProviderError("response_too_large", "TTS audio exceeded the byte limit")
        duration = _wav_duration(audio)
        return SpeechResult(
            audio,
            "openai",
            preset.model,
            preset.voice,
            elapsed,
            duration,
            len(value) * price / 1_000_000,
        )


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
        price = self.settings.providers.cloud_stt_price_per_minute_usd
        if price is None:
            raise SpeechProviderError("pricing_unknown", "paid STT pricing is not configured")
        if not audio or len(audio) > 8 * 1024 * 1024 or not 0 < duration_seconds <= 120:
            raise SpeechProviderError("audio_limit", "STT audio exceeds its limit")
        boundary = "butters-" + secrets.token_hex(12)
        body = _multipart(
            boundary,
            {"model": self.settings.providers.cloud_stt_model, "response_format": "json"},
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
            raise SpeechProviderError("upstream_status", f"OpenAI returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
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
            self.settings.providers.cloud_stt_model,
            time.perf_counter() - started,
            _safe_int(usage.get("input_tokens")),
            _safe_int(usage.get("output_tokens")),
            duration_seconds / 60 * price,
        )


def _validate_preset(preset: VoicePreset) -> None:
    if not preset.name or len(preset.name) > 64 or not all(
        character.isalnum() or character in " _-" for character in preset.name
    ):
        raise SpeechProviderError("invalid_preset", "preset name is invalid")
    if preset.provider not in {"local", "openai"}:
        raise SpeechProviderError("invalid_preset", "preset provider is invalid")
    if not preset.model or len(preset.model) > 128 or not preset.voice or len(preset.voice) > 128:
        raise SpeechProviderError("invalid_preset", "preset model or voice is invalid")
    if not 0.25 <= preset.speed <= 4.0 or len(preset.instructions) > 1000:
        raise SpeechProviderError("invalid_preset", "preset parameters exceed their limits")


def _speech_to_wav(speech: SynthesizedSpeech) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(speech.sample_rate)
        target.writeframes(speech.pcm_s16le)
    return output.getvalue()


def _wav_duration(audio: bytes) -> float:
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            return source.getnframes() / source.getframerate()
    except (wave.Error, EOFError, ZeroDivisionError) as exc:
        raise SpeechProviderError("malformed_audio", "TTS returned malformed WAV audio") from exc


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
