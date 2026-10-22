"""Reviewed OpenAI audio model and lifecycle constants."""

from __future__ import annotations

# ``openai`` remains the traditional Speech API provider for compatibility
# with deployed profiles. Realtime is a separate provider and API path.
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_REALTIME_TTS_MODEL = "gpt-realtime-2.1-mini"
OPENAI_STT_MODEL = "gpt-transcribe"

OPENAI_TTS_SHUTDOWN_DATE = "2027-01-06"
OPENAI_STT_SHUTDOWN_DATE = "2027-02-26"

DEPRECATED_OPENAI_TTS_MODELS = frozenset(
    {
        "tts-1",
        "tts-1-hd",
        "gpt-4o-mini-tts",
        "gpt-4o-mini-tts-2025-03-20",
        "gpt-4o-mini-tts-2025-12-15",
    }
)

# These deprecated models remain supported by /v1/audio/speech until the
# published shutdown date. They are selectable so existing profiles retain
# their behaviour; the UI note makes the lifecycle explicit.
OPENAI_SPEECH_TTS_MODELS = (
    "gpt-4o-mini-tts",
    "gpt-4o-mini-tts-2025-12-15",
    "tts-1",
    "tts-1-hd",
)
DEPRECATED_OPENAI_STT_MODELS = frozenset(
    {
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-mini-transcribe",
        "gpt-4o-transcribe-diarize",
    }
)

# Realtime and Speech endpoint voice sets aren't identical. These are the
# current Realtime voices documented for the replacement model.
OPENAI_REALTIME_VOICES = (
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
)


def migrated_realtime_voice(voice: object) -> str:
    """Map an explicitly requested Realtime migration to a supported voice."""

    return str(voice) if voice in OPENAI_REALTIME_VOICES else "alloy"
