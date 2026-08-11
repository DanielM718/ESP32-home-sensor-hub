"""Non-secret configuration for deterministic routing, integrations, and TTS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from butters.config import ConfigError, subsystem_root


def default_assistant_config_path() -> Path:
    return subsystem_root() / "config" / "assistant.toml"


@dataclass(frozen=True, slots=True)
class IntegrationSettings:
    dashboard_url: str = "http://127.0.0.1:8080"
    timeout_seconds: float = 4.0
    cache_seconds: float = 5.0
    max_response_bytes: int = 2 * 1024 * 1024

    def validated(self) -> IntegrationSettings:
        if not self.dashboard_url.startswith(("http://", "https://")):
            raise ConfigError("integration.dashboard_url must be HTTP or HTTPS")
        if not 0.1 <= self.timeout_seconds <= 30.0:
            raise ConfigError("integration.timeout_seconds must be 0.1 to 30")
        if not 0.0 <= self.cache_seconds <= 300.0:
            raise ConfigError("integration.cache_seconds must be 0 to 300")
        if not 1024 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise ConfigError("integration.max_response_bytes must be 1024 to 16777216")
        return self


@dataclass(frozen=True, slots=True)
class TTSSettings:
    model_dir: Path
    num_threads: int = 2
    speed: float = 1.0
    max_text_chars: int = 500

    def validated(self) -> TTSSettings:
        if not 1 <= self.num_threads <= 4:
            raise ConfigError("tts.num_threads must be between 1 and 4")
        if not 0.5 <= self.speed <= 2.0:
            raise ConfigError("tts.speed must be between 0.5 and 2.0")
        if not 1 <= self.max_text_chars <= 2000:
            raise ConfigError("tts.max_text_chars must be between 1 and 2000")
        return self


@dataclass(frozen=True, slots=True)
class EntitySettings:
    entity_id: str
    display_name: str
    sensor_type: str
    source_id: str
    aliases: tuple[str, ...]
    groups: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AssistantSettings:
    integration: IntegrationSettings
    tts: TTSSettings
    entities: tuple[EntitySettings, ...]


def load_assistant_settings(path: Path | None = None) -> AssistantSettings:
    config_path = (path or default_assistant_config_path()).expanduser()
    try:
        with config_path.open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError as exc:
        raise ConfigError(f"assistant configuration not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load assistant configuration: {exc}") from exc

    integration_table = _table(data, "integration")
    integration = IntegrationSettings(
        dashboard_url=str(
            integration_table.get("dashboard_url", "http://127.0.0.1:8080")
        ).rstrip("/"),
        timeout_seconds=float(integration_table.get("timeout_seconds", 4.0)),
        cache_seconds=float(integration_table.get("cache_seconds", 5.0)),
        max_response_bytes=int(
            integration_table.get("max_response_bytes", 2 * 1024 * 1024)
        ),
    ).validated()

    tts_table = _table(data, "tts")
    model_value = str(
        tts_table.get("model_dir", "../models/vits-piper-en_US-kathleen-low")
    )
    model_dir = Path(model_value).expanduser()
    if not model_dir.is_absolute():
        model_dir = config_path.resolve().parent / model_dir
    tts = TTSSettings(
        model_dir=model_dir,
        num_threads=int(tts_table.get("num_threads", 2)),
        speed=float(tts_table.get("speed", 1.0)),
        max_text_chars=int(tts_table.get("max_text_chars", 500)),
    ).validated()

    raw_entities = data.get("entities", [])
    if not isinstance(raw_entities, list) or not raw_entities:
        raise ConfigError("assistant configuration requires at least one [[entities]]")
    entities = tuple(_entity(item, index) for index, item in enumerate(raw_entities))
    ids = [entity.entity_id for entity in entities]
    if len(ids) != len(set(ids)):
        raise ConfigError("assistant entity IDs must be unique")
    source_keys = [(entity.sensor_type, entity.source_id) for entity in entities]
    if len(source_keys) != len(set(source_keys)):
        raise ConfigError("assistant entity source mappings must be unique")
    return AssistantSettings(integration=integration, tts=tts, entities=entities)


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _entity(value: Any, index: int) -> EntitySettings:
    if not isinstance(value, dict):
        raise ConfigError(f"entities[{index}] must be a TOML table")
    required = ("id", "display_name", "sensor_type", "source_id")
    missing = [name for name in required if not str(value.get(name, "")).strip()]
    if missing:
        raise ConfigError(f"entities[{index}] missing: {', '.join(missing)}")
    sensor_type = str(value["sensor_type"])
    if sensor_type not in {"environment", "air_quality"}:
        raise ConfigError(f"entities[{index}].sensor_type is unsupported")
    aliases = _string_tuple(value.get("aliases", []), f"entities[{index}].aliases")
    groups = _string_tuple(value.get("groups", []), f"entities[{index}].groups")
    return EntitySettings(
        entity_id=str(value["id"]),
        display_name=str(value["display_name"]),
        sensor_type=sensor_type,
        source_id=str(value["source_id"]),
        aliases=aliases,
        groups=groups,
    )


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ConfigError(f"{label} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)
