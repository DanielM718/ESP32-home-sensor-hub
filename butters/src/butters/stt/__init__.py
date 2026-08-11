"""Local streaming speech-recognition components."""

from butters.stt.model import StreamingSTTEngine, STTEngineError
from butters.stt.normalization import DomainVocabulary, normalize_transcript
from butters.stt.session import (
    StreamingTranscriber,
    TranscriptionEvent,
    UtteranceResult,
)

__all__ = [
    "DomainVocabulary",
    "STTEngineError",
    "StreamingSTTEngine",
    "StreamingTranscriber",
    "TranscriptionEvent",
    "UtteranceResult",
    "normalize_transcript",
]
