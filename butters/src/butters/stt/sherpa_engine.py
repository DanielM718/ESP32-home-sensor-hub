"""sherpa-onnx implementation of the recognizer-neutral STT interface."""

from __future__ import annotations

import sys
import time
from array import array
from pathlib import Path
from typing import Any

from butters.audio.model import INTERNAL_AUDIO_FORMAT, AudioFrame
from butters.stt.model import StreamingSTTEngine, STTEngineError

MODEL_FILES = {
    "tokens": "tokens.txt",
    "encoder": "encoder-epoch-99-avg-1.int8.onnx",
    "decoder": "decoder-epoch-99-avg-1.int8.onnx",
    "joiner": "joiner-epoch-99-avg-1.int8.onnx",
}


class SherpaOnnxStreamingSTT(StreamingSTTEngine):
    """True streaming transducer inference using sherpa-onnx."""

    def __init__(
        self,
        model_dir: Path,
        *,
        num_threads: int = 1,
        decoding_method: str = "greedy_search",
        sherpa_endpoint_enabled: bool = False,
        sherpa_endpoint_silence_seconds: float = 2.0,
        max_utterance_seconds: float = 20.0,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.decoding_method = decoding_method
        self.sherpa_endpoint_enabled = sherpa_endpoint_enabled
        paths = {name: self.model_dir / filename for name, filename in MODEL_FILES.items()}
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise STTEngineError(
                "streaming STT model is incomplete; missing: " + ", ".join(missing)
            )
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise STTEngineError(
                "sherpa-onnx is not installed; run "
                "butters/.venv/bin/python -m pip install -r "
                "butters/requirements-stt.txt"
            ) from exc

        started = time.perf_counter()
        try:
            self._recognizer: Any | None = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(paths["tokens"]),
                encoder=str(paths["encoder"]),
                decoder=str(paths["decoder"]),
                joiner=str(paths["joiner"]),
                num_threads=num_threads,
                sample_rate=INTERNAL_AUDIO_FORMAT.sample_rate,
                feature_dim=80,
                enable_endpoint_detection=sherpa_endpoint_enabled,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=sherpa_endpoint_silence_seconds,
                rule3_min_utterance_length=max_utterance_seconds,
                decoding_method=decoding_method,
                max_active_paths=4,
                provider="cpu",
            )
        except Exception as exc:
            raise STTEngineError(f"cannot load sherpa-onnx model: {exc}") from exc
        self._initialization_seconds = time.perf_counter() - started
        self._stream: Any | None = None
        self._partial = ""
        self._active = False

    @property
    def initialization_seconds(self) -> float:
        return self._initialization_seconds

    @property
    def model_bytes(self) -> int:
        return sum((self.model_dir / filename).stat().st_size for filename in MODEL_FILES.values())

    def _require_open(self) -> Any:
        if self._recognizer is None:
            raise STTEngineError("recognizer has been closed")
        return self._recognizer

    def _require_active(self) -> tuple[Any, Any]:
        recognizer = self._require_open()
        if not self._active or self._stream is None:
            raise STTEngineError("no active utterance; call start_utterance() first")
        return recognizer, self._stream

    def start_utterance(self) -> None:
        recognizer = self._require_open()
        if self._active:
            raise STTEngineError("an utterance is already active")
        self._stream = recognizer.create_stream()
        self._partial = ""
        self._active = True

    @staticmethod
    def _normalized_samples(frame: AudioFrame) -> array:
        samples = array("h")
        samples.frombytes(frame.pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        return array("f", (sample / 32768.0 for sample in samples))

    @staticmethod
    def _result_text(recognizer: Any, stream: Any) -> str:
        result = recognizer.get_result(stream)
        text = result.text if hasattr(result, "text") else str(result)
        return text.strip()

    def accept_audio(self, frame: AudioFrame) -> str | None:
        recognizer, stream = self._require_active()
        stream.accept_waveform(
            INTERNAL_AUDIO_FORMAT.sample_rate,
            self._normalized_samples(frame),
        )
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        text = self._result_text(recognizer, stream)
        if text == self._partial:
            return None
        self._partial = text
        return text

    def get_partial_transcript(self) -> str:
        return self._partial

    def endpoint_detected(self) -> bool:
        if (
            not self.sherpa_endpoint_enabled
            or not self._active
            or self._stream is None
        ):
            return False
        return bool(self._require_open().is_endpoint(self._stream))

    def finalize(self) -> str:
        recognizer, stream = self._require_active()
        stream.input_finished()
        while recognizer.is_ready(stream):
            recognizer.decode_stream(stream)
        self._partial = self._result_text(recognizer, stream)
        self._active = False
        return self._partial

    def reset(self) -> None:
        self._stream = None
        self._partial = ""
        self._active = False

    def close(self) -> None:
        self.reset()
        self._recognizer = None
