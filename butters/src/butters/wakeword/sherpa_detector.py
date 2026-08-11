"""sherpa-onnx open-vocabulary streaming keyword spotter."""

from __future__ import annotations

import sys
import time
from array import array
from pathlib import Path
from typing import Any

from butters.audio.model import INTERNAL_AUDIO_FORMAT, AudioFrame
from butters.wakeword.model import WakeDetection, WakeWordDetector, WakeWordError


class SherpaOnnxWakeWordDetector(WakeWordDetector):
    """Small local KWS model using a replaceable tokenized keyword file.

    sherpa-onnx exposes the configured probability threshold but not the
    winning path's confidence score. ``WakeDetection.confidence`` is therefore
    explicitly ``None`` instead of inventing a score.
    """

    def __init__(
        self,
        model_dir: Path,
        keywords_path: Path,
        *,
        num_threads: int = 1,
        chunk_size: int = 8,
        score: float = 1.5,
        threshold: float = 0.25,
    ) -> None:
        self.model_dir = Path(model_dir)
        self.keywords_path = Path(keywords_path)
        self.num_threads = num_threads
        self.chunk_size = chunk_size
        self.score = score
        self.threshold = threshold
        if chunk_size not in {8, 16}:
            raise WakeWordError("sherpa keyword chunk size must be 8 or 16")
        files = {
            "tokens": self.model_dir / "tokens.txt",
            "encoder": self.model_dir
            / f"encoder-epoch-13-avg-2-chunk-{chunk_size}-left-64.int8.onnx",
            "decoder": self.model_dir
            / f"decoder-epoch-13-avg-2-chunk-{chunk_size}-left-64.onnx",
            "joiner": self.model_dir
            / f"joiner-epoch-13-avg-2-chunk-{chunk_size}-left-64.int8.onnx",
            "keywords": self.keywords_path,
        }
        missing = [str(path) for path in files.values() if not path.is_file()]
        if missing:
            raise WakeWordError(
                "wake-word model/config is incomplete; missing: " + ", ".join(missing)
            )
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise WakeWordError(
                "sherpa-onnx is not installed in the Butters environment"
            ) from exc

        started = time.perf_counter()
        try:
            self._spotter: Any | None = sherpa_onnx.KeywordSpotter(
                tokens=str(files["tokens"]),
                encoder=str(files["encoder"]),
                decoder=str(files["decoder"]),
                joiner=str(files["joiner"]),
                keywords_file=str(files["keywords"]),
                num_threads=num_threads,
                sample_rate=INTERNAL_AUDIO_FORMAT.sample_rate,
                feature_dim=80,
                max_active_paths=4,
                keywords_score=score,
                keywords_threshold=threshold,
                num_trailing_blanks=1,
                provider="cpu",
            )
        except Exception as exc:
            raise WakeWordError(f"cannot load sherpa-onnx wake model: {exc}") from exc
        self._initialization_seconds = time.perf_counter() - started
        self._model_bytes = sum(
            path.stat().st_size for name, path in files.items() if name != "keywords"
        )
        self._stream: Any | None = self._spotter.create_stream()
        self._audio_seconds = 0.0

    @property
    def initialization_seconds(self) -> float:
        return self._initialization_seconds

    @property
    def model_bytes(self) -> int:
        return self._model_bytes

    @staticmethod
    def _normalized_samples(frame: AudioFrame) -> array:
        samples = array("h")
        samples.frombytes(frame.pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        return array("f", (sample / 32768.0 for sample in samples))

    def _require_open(self) -> tuple[Any, Any]:
        if self._spotter is None or self._stream is None:
            raise WakeWordError("wake-word detector has been closed")
        return self._spotter, self._stream

    def accept_audio(self, frame: AudioFrame) -> WakeDetection | None:
        spotter, stream = self._require_open()
        stream.accept_waveform(
            INTERNAL_AUDIO_FORMAT.sample_rate,
            self._normalized_samples(frame),
        )
        self._audio_seconds += frame.duration_seconds
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            # Reading the underlying result once preserves tokens/timestamps;
            # the public helpers each perform a separate destructive read.
            result = spotter.keyword_spotter.get_result(stream)
            keyword = result.keyword.strip()
            if not keyword:
                continue
            timestamps = tuple(float(value) for value in result.timestamps)
            model_latency = None
            if timestamps:
                model_latency = max(0.0, self._audio_seconds - timestamps[-1])
            return WakeDetection(
                keyword=keyword.replace("_", " "),
                confidence=None,
                threshold=self.threshold,
                model_latency_seconds=model_latency,
                tokens=tuple(result.tokens),
                token_timestamps=timestamps,
            )
        return None

    def reset(self) -> None:
        if self._spotter is None:
            return
        self._stream = self._spotter.create_stream()
        self._audio_seconds = 0.0

    def close(self) -> None:
        self._stream = None
        self._spotter = None
