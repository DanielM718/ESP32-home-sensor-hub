"""Configuration loading for Butters audio and streaming STT."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import tomllib


class ConfigError(ValueError):
    """Raised when an audio configuration value is invalid."""


def subsystem_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_stt_model_dir() -> Path:
    return (
        subsystem_root()
        / "models"
        / "sherpa-onnx-streaming-zipformer-en-2023-06-21"
    )


def default_vocabulary_path() -> Path:
    return subsystem_root() / "config" / "domain_vocabulary.toml"


def default_wakeword_model_dir() -> Path:
    return (
        subsystem_root()
        / "models"
        / "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
    )


def default_wakeword_keywords_path() -> Path:
    return subsystem_root() / "config" / "wakewords.txt"


@dataclass(frozen=True, slots=True)
class AudioSettings:
    source: str = "alsa"
    frame_ms: int = 20
    preroll_ms: int = 800
    alsa_device: str = ""
    alsa_video_warmup_device: str = ""
    wave_path: Path | None = None
    wave_realtime: bool = False
    wave_loop: bool = False
    vad_enabled: bool = True
    vad_threshold_dbfs: float = -42.0
    vad_attack_ms: int = 40
    vad_release_ms: int = 300
    clip_threshold: float = 0.98

    def validated(self) -> AudioSettings:
        if self.source not in {"alsa", "wave"}:
            raise ConfigError("audio.source must be 'alsa' or 'wave'")
        if self.frame_ms not in {10, 20, 30}:
            raise ConfigError("audio.frame_ms must be 10, 20, or 30")
        if not 0 <= self.preroll_ms <= 5000:
            raise ConfigError("audio.preroll_ms must be between 0 and 5000")
        if self.vad_attack_ms < 0 or self.vad_release_ms < 0:
            raise ConfigError("VAD attack and release durations cannot be negative")
        if not -96.0 <= self.vad_threshold_dbfs <= 0.0:
            raise ConfigError("vad.threshold_dbfs must be between -96 and 0")
        if not 0.5 <= self.clip_threshold <= 1.0:
            raise ConfigError("vad.clip_threshold must be between 0.5 and 1.0")
        return self


@dataclass(frozen=True, slots=True)
class STTSettings:
    model_dir: Path = field(default_factory=default_stt_model_dir)
    num_threads: int = 1
    decoding_method: str = "greedy_search"
    sherpa_endpoint_enabled: bool = False
    sherpa_endpoint_silence_ms: int = 2000
    max_utterance_seconds: float = 20.0
    vocabulary_path: Path = field(default_factory=default_vocabulary_path)

    @property
    def endpoint_silence_ms(self) -> int:
        """Deprecated milestone-3 alias for the Sherpa-only rule value."""

        return self.sherpa_endpoint_silence_ms

    def validated(self) -> STTSettings:
        if not 1 <= self.num_threads <= 8:
            raise ConfigError("stt.num_threads must be between 1 and 8")
        if self.decoding_method not in {"greedy_search", "modified_beam_search"}:
            raise ConfigError(
                "stt.decoding_method must be 'greedy_search' or "
                "'modified_beam_search'"
            )
        if not 200 <= self.sherpa_endpoint_silence_ms <= 5000:
            raise ConfigError(
                "stt.sherpa_endpoint_silence_ms must be between 200 and 5000"
            )
        if not 1.0 <= self.max_utterance_seconds <= 120.0:
            raise ConfigError("stt.max_utterance_seconds must be between 1 and 120")
        return self


@dataclass(frozen=True, slots=True)
class WakeWordSettings:
    model_dir: Path = field(default_factory=default_wakeword_model_dir)
    keywords_path: Path = field(default_factory=default_wakeword_keywords_path)
    phrase: str = "Hey Butters"
    num_threads: int = 1
    chunk_size: int = 8
    score: float = 1.5
    threshold: float = 0.25

    def validated(self) -> WakeWordSettings:
        if not self.phrase.strip():
            raise ConfigError("wakeword.phrase cannot be empty")
        if not 1 <= self.num_threads <= 4:
            raise ConfigError("wakeword.num_threads must be between 1 and 4")
        if self.chunk_size not in {8, 16}:
            raise ConfigError("wakeword.chunk_size must be 8 or 16")
        if not 0.0 < self.score <= 10.0:
            raise ConfigError("wakeword.score must be greater than 0 and at most 10")
        if not 0.0 < self.threshold < 1.0:
            raise ConfigError("wakeword.threshold must be between 0 and 1")
        return self


@dataclass(frozen=True, slots=True)
class LiveSettings:
    no_speech_timeout_seconds: float = 4.0
    max_command_seconds: float = 20.0
    command_preroll_ms: int = 300
    acknowledge: bool = True
    playback_device: str = "plughw:CARD=Headphones,DEV=0"
    chime_volume: float = 0.18
    acknowledgement_guard_ms: int = 120
    audio_retry_seconds: float = 1.0
    max_audio_retries: int = 3
    provisional_endpoint_silence_ms: int = 1000
    hard_endpoint_silence_ms: int = 2000
    continuation_timeout_seconds: float = 12.0

    def validated(self) -> LiveSettings:
        if not 1.0 <= self.no_speech_timeout_seconds <= 30.0:
            raise ConfigError("live.no_speech_timeout_seconds must be 1 to 30")
        if not 1.0 <= self.max_command_seconds <= 120.0:
            raise ConfigError("live.max_command_seconds must be 1 to 120")
        if not 0 <= self.command_preroll_ms <= 1000:
            raise ConfigError("live.command_preroll_ms must be 0 to 1000")
        if not self.playback_device.strip():
            raise ConfigError("playback.device cannot be empty")
        if not 0.0 <= self.chime_volume <= 1.0:
            raise ConfigError("playback.chime_volume must be between 0 and 1")
        if not 0 <= self.acknowledgement_guard_ms <= 500:
            raise ConfigError("playback.acknowledgement_guard_ms must be 0 to 500")
        if not 0.0 <= self.audio_retry_seconds <= 30.0:
            raise ConfigError("live.audio_retry_seconds must be 0 to 30")
        if not 0 <= self.max_audio_retries <= 100:
            raise ConfigError("live.max_audio_retries must be 0 to 100")
        if not 500 <= self.provisional_endpoint_silence_ms <= 2000:
            raise ConfigError(
                "live.provisional_endpoint_silence_ms must be between 500 and 2000"
            )
        if not 1000 <= self.hard_endpoint_silence_ms <= 5000:
            raise ConfigError(
                "live.hard_endpoint_silence_ms must be between 1000 and 5000"
            )
        if self.hard_endpoint_silence_ms <= self.provisional_endpoint_silence_ms:
            raise ConfigError(
                "live.hard_endpoint_silence_ms must be greater than "
                "live.provisional_endpoint_silence_ms"
            )
        if not 3.0 <= self.continuation_timeout_seconds <= 60.0:
            raise ConfigError("live.continuation_timeout_seconds must be 3 to 60")
        return self


def default_local_config_path() -> Path:
    configured = os.environ.get("BUTTERS_AUDIO_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return subsystem_root() / "config" / "audio.local.toml"


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _path_value(value: Any, config_dir: Path) -> Path | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ConfigError("wave.path must be a string")
    path = Path(value).expanduser()
    return path if path.is_absolute() else config_dir / path


def _required_path_value(value: Any, config_dir: Path, default: Path) -> Path:
    path = _path_value(value, config_dir)
    return default if path is None else path


def _load_toml(config_path: Path, *, optional: bool) -> dict[str, Any]:
    if optional and not config_path.exists():
        return {}
    try:
        with config_path.open("rb") as config_file:
            return tomllib.load(config_file)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc


def load_settings(path: Path | None = None) -> AudioSettings:
    config_path = path or default_local_config_path()
    data = _load_toml(config_path, optional=path is None)

    audio = _table(data, "audio")
    alsa = _table(data, "alsa")
    wave = _table(data, "wave")
    vad = _table(data, "vad")
    config_dir = config_path.resolve().parent
    settings = AudioSettings(
        source=str(audio.get("source", "alsa")),
        frame_ms=int(audio.get("frame_ms", 20)),
        preroll_ms=int(audio.get("preroll_ms", 800)),
        alsa_device=str(alsa.get("device", "")),
        alsa_video_warmup_device=str(alsa.get("video_warmup_device", "")),
        wave_path=_path_value(wave.get("path"), config_dir),
        wave_realtime=bool(wave.get("realtime", False)),
        wave_loop=bool(wave.get("loop", False)),
        vad_enabled=bool(vad.get("enabled", True)),
        vad_threshold_dbfs=float(vad.get("threshold_dbfs", -42.0)),
        vad_attack_ms=int(vad.get("attack_ms", 40)),
        vad_release_ms=int(vad.get("release_ms", 300)),
        clip_threshold=float(vad.get("clip_threshold", 0.98)),
    )
    return settings.validated()


def load_stt_settings(path: Path | None = None) -> STTSettings:
    config_path = path or default_local_config_path()
    data = _load_toml(config_path, optional=path is None)
    stt = _table(data, "stt")
    config_dir = config_path.resolve().parent
    settings = STTSettings(
        model_dir=_required_path_value(
            stt.get("model_dir"), config_dir, default_stt_model_dir()
        ),
        num_threads=int(stt.get("num_threads", 1)),
        decoding_method=str(stt.get("decoding_method", "greedy_search")),
        sherpa_endpoint_enabled=bool(stt.get("sherpa_endpoint_enabled", False)),
        # Accept the milestone-3 name only as an internal Sherpa rule setting.
        # Live acoustic endpoint timing is loaded independently below.
        sherpa_endpoint_silence_ms=int(
            stt.get(
                "sherpa_endpoint_silence_ms",
                stt.get("endpoint_silence_ms", 2000),
            )
        ),
        max_utterance_seconds=float(stt.get("max_utterance_seconds", 20.0)),
        vocabulary_path=_required_path_value(
            stt.get("vocabulary_path"), config_dir, default_vocabulary_path()
        ),
    )
    return settings.validated()


def load_wakeword_settings(path: Path | None = None) -> WakeWordSettings:
    config_path = path or default_local_config_path()
    data = _load_toml(config_path, optional=path is None)
    wakeword = _table(data, "wakeword")
    config_dir = config_path.resolve().parent
    settings = WakeWordSettings(
        model_dir=_required_path_value(
            wakeword.get("model_dir"), config_dir, default_wakeword_model_dir()
        ),
        keywords_path=_required_path_value(
            wakeword.get("keywords_path"),
            config_dir,
            default_wakeword_keywords_path(),
        ),
        phrase=str(wakeword.get("phrase", "Hey Butters")),
        num_threads=int(wakeword.get("num_threads", 1)),
        chunk_size=int(wakeword.get("chunk_size", 8)),
        score=float(wakeword.get("score", 1.5)),
        threshold=float(wakeword.get("threshold", 0.25)),
    )
    return settings.validated()


def load_live_settings(path: Path | None = None) -> LiveSettings:
    config_path = path or default_local_config_path()
    data = _load_toml(config_path, optional=path is None)
    live = _table(data, "live")
    playback = _table(data, "playback")
    settings = LiveSettings(
        no_speech_timeout_seconds=float(
            live.get("no_speech_timeout_seconds", 4.0)
        ),
        max_command_seconds=float(live.get("max_command_seconds", 20.0)),
        command_preroll_ms=int(live.get("command_preroll_ms", 300)),
        acknowledge=bool(playback.get("acknowledge", True)),
        playback_device=str(
            playback.get("device", "plughw:CARD=Headphones,DEV=0")
        ),
        chime_volume=float(playback.get("chime_volume", 0.18)),
        acknowledgement_guard_ms=int(
            playback.get("acknowledgement_guard_ms", 120)
        ),
        audio_retry_seconds=float(live.get("audio_retry_seconds", 1.0)),
        max_audio_retries=int(live.get("max_audio_retries", 3)),
        provisional_endpoint_silence_ms=int(
            live.get("provisional_endpoint_silence_ms", 1000)
        ),
        hard_endpoint_silence_ms=int(
            live.get("hard_endpoint_silence_ms", 2000)
        ),
        continuation_timeout_seconds=float(
            live.get("continuation_timeout_seconds", 12.0)
        ),
    )
    return settings.validated()


def with_stt_overrides(
    settings: STTSettings,
    *,
    model_dir: Path | None = None,
    num_threads: int | None = None,
) -> STTSettings:
    changes: dict[str, Any] = {}
    if model_dir is not None:
        changes["model_dir"] = model_dir.expanduser()
    if num_threads is not None:
        changes["num_threads"] = num_threads
    return replace(settings, **changes).validated()


def with_wakeword_overrides(
    settings: WakeWordSettings,
    *,
    model_dir: Path | None = None,
    threshold: float | None = None,
    score: float | None = None,
    num_threads: int | None = None,
) -> WakeWordSettings:
    changes: dict[str, Any] = {}
    if model_dir is not None:
        changes["model_dir"] = model_dir.expanduser()
    if threshold is not None:
        changes["threshold"] = threshold
    if score is not None:
        changes["score"] = score
    if num_threads is not None:
        changes["num_threads"] = num_threads
    return replace(settings, **changes).validated()


def with_overrides(
    settings: AudioSettings,
    *,
    source: str | None = None,
    input_value: str | None = None,
    realtime: bool | None = None,
    loop: bool | None = None,
) -> AudioSettings:
    selected_source = source or settings.source
    changes: dict[str, Any] = {"source": selected_source}
    if input_value is not None:
        if selected_source == "alsa":
            changes["alsa_device"] = input_value
        else:
            changes["wave_path"] = Path(input_value).expanduser()
    if realtime is not None:
        changes["wave_realtime"] = realtime
    if loop is not None:
        changes["wave_loop"] = loop
    return replace(settings, **changes).validated()
