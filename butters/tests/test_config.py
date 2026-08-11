from __future__ import annotations

from pathlib import Path

import pytest

from butters.config import (
    ConfigError,
    load_live_settings,
    load_settings,
    load_stt_settings,
    load_wakeword_settings,
    with_overrides,
    with_stt_overrides,
    with_wakeword_overrides,
)


def test_local_toml_selects_device_and_resolves_wave_path(tmp_path: Path) -> None:
    config_path = tmp_path / "audio.toml"
    config_path.write_text(
        """
[audio]
source = "alsa"
frame_ms = 30
preroll_ms = 900

[alsa]
device = "plughw:CARD=Camera,DEV=0"
video_warmup_device = "/dev/v4l/by-id/camera-video-index0"

[wave]
path = "fixtures/development.wav"
realtime = true

[vad]
threshold_dbfs = -38.5
attack_ms = 60
release_ms = 240
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings.alsa_device == "plughw:CARD=Camera,DEV=0"
    assert settings.alsa_video_warmup_device.endswith("camera-video-index0")
    assert settings.wave_path == tmp_path / "fixtures/development.wav"
    assert settings.frame_ms == 30
    assert settings.preroll_ms == 900
    assert settings.wave_realtime
    assert settings.vad_threshold_dbfs == -38.5

    overridden = with_overrides(
        settings,
        source="wave",
        input_value="/tmp/one-off.wav",
        realtime=False,
    )
    assert overridden.source == "wave"
    assert overridden.wave_path == Path("/tmp/one-off.wav")
    assert not overridden.wave_realtime


def test_invalid_frame_duration_is_rejected(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.toml"
    config_path.write_text("[audio]\nframe_ms = 25\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="10, 20, or 30"):
        load_settings(config_path)


def test_stt_config_resolves_paths_and_supports_safe_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "audio.toml"
    config_path.write_text(
        """
[stt]
model_dir = "models/streaming"
num_threads = 1
endpoint_silence_ms = 700
vocabulary_path = "vocabulary.toml"
""".strip(),
        encoding="utf-8",
    )

    settings = load_stt_settings(config_path)

    assert settings.model_dir == tmp_path / "models/streaming"
    assert settings.num_threads == 1
    assert settings.endpoint_silence_ms == 700
    assert settings.vocabulary_path == tmp_path / "vocabulary.toml"
    overridden = with_stt_overrides(settings, num_threads=2)
    assert overridden.num_threads == 2


def test_stt_thread_limit_is_validated(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid-stt.toml"
    config_path.write_text("[stt]\nnum_threads = 0\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="between 1 and 8"):
        load_stt_settings(config_path)


def test_wakeword_and_live_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "audio.toml"
    config_path.write_text(
        """
[wakeword]
model_dir = "models/kws"
keywords_path = "keywords.txt"
phrase = "Hey Butters"
chunk_size = 8
threshold = 0.3

[live]
no_speech_timeout_seconds = 3.5
command_preroll_ms = 260

[playback]
acknowledge = false
device = "plughw:CARD=Speaker,DEV=0"
acknowledgement_guard_ms = 90
""".strip(),
        encoding="utf-8",
    )

    wake = load_wakeword_settings(config_path)
    live = load_live_settings(config_path)

    assert wake.model_dir == tmp_path / "models/kws"
    assert wake.keywords_path == tmp_path / "keywords.txt"
    assert wake.threshold == 0.3
    assert with_wakeword_overrides(wake, threshold=0.4).threshold == 0.4
    assert live.no_speech_timeout_seconds == 3.5
    assert live.command_preroll_ms == 260
    assert not live.acknowledge
    assert live.acknowledgement_guard_ms == 90
