"""Reviewed OpenAI audio model lifecycle constants.

Keep retirement handling separate from the capability catalog: retired model
IDs must remain recognizable for configuration migration and useful errors,
but must never be advertised as selectable capabilities.
"""

from __future__ import annotations

OPENAI_TTS_MODEL = "gpt-realtime-2.1-mini"
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
    """Preserve a legacy voice when Realtime supports it, otherwise use Alloy."""

    return str(voice) if voice in OPENAI_REALTIME_VOICES else "alloy"
