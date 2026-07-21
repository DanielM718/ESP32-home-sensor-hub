"""Configuration loading for backend services."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ConfigError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    keepalive_seconds: int
    client_id: str
    username: str
    password: str
    sensor_topic: str
    air_topic: str
    qos: int
    max_payload_bytes: int


@dataclass(frozen=True)
class InfluxSettings:
    url: str
    org: str
    bucket: str
    write_token: str
    read_token: str
    live_bucket: str = "environment_live"


@dataclass(frozen=True)
class AirQualitySettings:
    expected_publish_seconds: int = 5
    stale_after_seconds: int = 20
    rolling_minimum_coverage_percent: int = 75
    recovery_lookback_minutes: int = 30


@dataclass(frozen=True)
class AppSettings:
    log_level: str
    node_stale_after_seconds: int
    mqtt: MqttSettings
    influx: InfluxSettings
    air_quality: AirQualitySettings = field(default_factory=AirQualitySettings)


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> AppSettings:
    """Load service configuration from environment variables and `.env`."""

    if env_file is not None:
        load_dotenv(env_file)

    write_token = _get_first_required("INFLUXDB_WRITE_TOKEN", "INFLUXDB_TOKEN")
    read_token = _get_env("INFLUXDB_READ_TOKEN", os.getenv("INFLUXDB_TOKEN", ""))

    return AppSettings(
        log_level=_get_env("LOG_LEVEL", "INFO"),
        node_stale_after_seconds=_get_int(
            "NODE_STALE_AFTER_SECONDS", 1800, min_value=60, max_value=604800
        ),
        mqtt=MqttSettings(
            host=_get_env("MQTT_HOST", "127.0.0.1"),
            port=_get_int("MQTT_PORT", 1883, min_value=1, max_value=65535),
            keepalive_seconds=_get_int(
                "MQTT_KEEPALIVE_SECONDS", 60, min_value=5, max_value=3600
            ),
            client_id=_get_env("MQTT_CLIENT_ID", "home-sensor-bridge"),
            username=_get_required("MQTT_USERNAME"),
            password=_get_required("MQTT_PASSWORD"),
            sensor_topic=_get_env("MQTT_SENSOR_TOPIC", "home/sensors/+"),
            air_topic=_get_env("MQTT_AIR_TOPIC", "home/air/+"),
            qos=_get_int("MQTT_QOS", 1, min_value=0, max_value=2),
            max_payload_bytes=_get_int(
                "MQTT_MAX_PAYLOAD_BYTES", 4096, min_value=256, max_value=65536
            ),
        ),
        influx=InfluxSettings(
            url=_get_env("INFLUXDB_URL", "http://127.0.0.1:8086"),
            org=_get_env("INFLUXDB_ORG", "home"),
            bucket=_get_env("INFLUXDB_BUCKET", "environment"),
            write_token=write_token,
            read_token=read_token,
            live_bucket=_get_env("INFLUXDB_LIVE_BUCKET", "environment_live"),
        ),
        air_quality=AirQualitySettings(
            expected_publish_seconds=_get_int(
                "SEN66_EXPECTED_PUBLISH_SECONDS", 5, min_value=1, max_value=60
            ),
            stale_after_seconds=_get_int(
                "SEN66_STALE_AFTER_SECONDS", 20, min_value=5, max_value=3600
            ),
            rolling_minimum_coverage_percent=_get_int(
                "SEN66_24H_MINIMUM_COVERAGE_PERCENT", 75, min_value=50, max_value=100
            ),
            recovery_lookback_minutes=_get_int(
                "SEN66_RECOVERY_LOOKBACK_MINUTES", 30, min_value=15, max_value=180
            ),
        ),
    )


def configure_logging(level_name: str) -> None:
    """Configure process-wide logging for systemd/journald output."""

    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        raise ConfigError(f"Invalid LOG_LEVEL: {level_name}")

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _get_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _get_first_required(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    joined = " or ".join(names)
    raise ConfigError(f"Missing required environment variable: {joined}")


def _get_env(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value if value else default


def _get_int(
    name: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value.strip() == "":
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc

    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ConfigError(f"{name} must be <= {max_value}")
    return value
