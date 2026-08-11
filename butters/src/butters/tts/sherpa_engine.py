"""Piper-compatible VITS synthesis through the existing sherpa-onnx runtime."""

from __future__ import annotations

import math
import sys
import time
from array import array
from pathlib import Path
from typing import Any

from butters.tts.model import SynthesizedSpeech, TextToSpeechEngine, TTSError


class SherpaOnnxPiperTTS(TextToSpeechEngine):
    def __init__(
        self,
        model_dir: Path,
        *,
        num_threads: int = 2,
        speed: float = 1.0,
        max_text_chars: int = 500,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.speed = speed
        self.max_text_chars = max_text_chars
        models = sorted(self.model_dir.glob("*.onnx"))
        if len(models) != 1:
            raise TTSError(
                f"TTS model directory must contain exactly one ONNX file; found {len(models)}"
            )
        self._model_path = models[0]
        self._tokens_path = self.model_dir / "tokens.txt"
        self._data_dir = self.model_dir / "espeak-ng-data"
        required = (self._model_path, self._tokens_path, self._data_dir)
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise TTSError(f"TTS model is incomplete; missing: {', '.join(missing)}")
        if not 1 <= num_threads <= 4:
            raise TTSError("TTS threads must be between 1 and 4")
        started = time.perf_counter()
        try:
            import sherpa_onnx

            vits = sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(self._model_path),
                tokens=str(self._tokens_path),
                data_dir=str(self._data_dir),
            )
            model = sherpa_onnx.OfflineTtsModelConfig(
                vits=vits,
                num_threads=num_threads,
                provider="cpu",
            )
            config = sherpa_onnx.OfflineTtsConfig(
                model=model,
                max_num_sentences=1,
                silence_scale=0.2,
            )
            if not config.validate():
                raise TTSError("sherpa-onnx rejected the TTS configuration")
            self._engine: Any | None = sherpa_onnx.OfflineTts(config)
        except TTSError:
            raise
        except (ImportError, RuntimeError, ValueError) as exc:
            raise TTSError(f"cannot initialize local Piper voice: {exc}") from exc
        self._initialization_seconds = time.perf_counter() - started

    @property
    def initialization_seconds(self) -> float:
        return self._initialization_seconds

    @property
    def model_bytes(self) -> int:
        return sum(
            path.stat().st_size for path in self.model_dir.rglob("*") if path.is_file()
        )

    def synthesize(self, text: str) -> SynthesizedSpeech:
        value = " ".join(text.split())
        if not value:
            raise TTSError("TTS text cannot be empty")
        if len(value) > self.max_text_chars:
            raise TTSError(f"TTS text exceeds {self.max_text_chars} characters")
        if self._engine is None:
            raise TTSError("TTS engine is closed")
        started = time.perf_counter()
        try:
            audio = self._engine.generate(text=value, sid=0, speed=self.speed)
        except (RuntimeError, ValueError) as exc:
            raise TTSError(f"speech synthesis failed: {exc}") from exc
        elapsed = time.perf_counter() - started
        sample_rate = int(audio.sample_rate)
        if sample_rate <= 0:
            raise TTSError("TTS returned an invalid sample rate")
        pcm = array(
            "h",
            (
                round(max(-1.0, min(1.0, _finite_sample(sample))) * 32767)
                for sample in audio.samples
            ),
        )
        if sys.byteorder != "little":
            pcm.byteswap()
        if not pcm:
            raise TTSError("TTS returned no audio")
        return SynthesizedSpeech(pcm.tobytes(), sample_rate, elapsed)

    def close(self) -> None:
        self._engine = None


def _finite_sample(value: object) -> float:
    try:
        sample = float(value)
    except (TypeError, ValueError) as exc:
        raise TTSError("TTS returned a non-numeric sample") from exc
    return sample if math.isfinite(sample) else 0.0
