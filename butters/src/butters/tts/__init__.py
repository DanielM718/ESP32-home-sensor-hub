"""Replaceable local speech synthesis and output adapters."""

from butters.tts.model import SynthesizedSpeech, TextToSpeechEngine
from butters.tts.output import WaveFileOutput

__all__ = ["SynthesizedSpeech", "TextToSpeechEngine", "WaveFileOutput"]
