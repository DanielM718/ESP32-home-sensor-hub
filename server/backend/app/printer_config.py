"""Non-secret printer observer configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import tomllib

from app.config import ConfigError

ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
KNOWN_ENTITY_KEYS = frozenset(
    {
        "online",
        "print_status",
        "current_stage",
        "job_id",
        "job_name",
        "progress_percent",
        "remaining_time",
        "current_layer",
        "total_layers",
        "print_started_at",
        "print_finished_at",
        "expected_finished_at",
        "nozzle_1_temperature",
        "nozzle_1_target",
        "nozzle_2_temperature",
        "nozzle_2_target",
        "bed_temperature",
        "bed_target",
        "chamber_temperature",
        "chamber_target",
        "active_tool",
        "active_material",
        "active_filament",
        "ams_state",
        "ams_slot",
        "print_source",
        "firmware_version",
        "gcode_filename",
        "print_bed_type",
        "print_weight_grams",
        "print_length_meters",
        "nozzle_1_type",
        "nozzle_1_size_mm",
        "nozzle_2_type",
        "nozzle_2_size_mm",
        "cooling_fan_percent",
        "auxiliary_fan_percent",
        "chamber_fan_percent",
        "heatbreak_fan_percent",
        "wifi_signal_dbm",
        "mqtt_connection_mode",
        "mqtt_encryption",
        "hybrid_mqtt_control_blocked",
        "developer_lan_mode",
        "ha_bambulab_estimated_usage_hours",
        "printer_reported_lifetime_hours",
        "cover_image",
    }
)

KNOWN_AMS_ENTITY_KEYS = frozenset(
    {
        "active",
        "humidity_percent",
        "humidity_index",
        "temperature",
        "drying",
        "remaining_drying_time",
    }
)


@dataclass(frozen=True, slots=True)
class AmsObserverSettings:
    ams_id: str
    model: str
    entities: dict[str, str]
    tray_entities: tuple[str, ...] = ()

    def validated(self) -> AmsObserverSettings:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", self.ams_id):
            raise ConfigError("ams id must be a stable lowercase slug")
        if not self.model.strip():
            raise ConfigError("ams model cannot be empty")
        unknown = set(self.entities) - KNOWN_AMS_ENTITY_KEYS
        if unknown:
            raise ConfigError(f"unknown AMS mappings: {', '.join(sorted(unknown))}")
        values = tuple(self.entities.values()) + tuple(self.tray_entities)
        if any(not ENTITY_ID_RE.fullmatch(value) for value in values):
            raise ConfigError("AMS mappings must be Home Assistant entity IDs")
        if len(set(values)) != len(values):
            raise ConfigError("AMS entity mappings must be distinct")
        return self


@dataclass(frozen=True, slots=True)
class MaintenanceTaskSettings:
    task_id: str
    name: str
    description: str = ""
    interval_hours: float | None = None
    warning_hours: float = 0.0
    interval_prints: int | None = None
    warning_prints: int = 0
    interval_days: int | None = None
    warning_days: int = 0
    due_when: str = "any"
    notes: str = ""
    source: str = "user_configured"
    enabled: bool = True

    def validated(self) -> MaintenanceTaskSettings:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", self.task_id):
            raise ConfigError("maintenance task id must be a lowercase slug")
        if not self.name.strip():
            raise ConfigError("maintenance task name cannot be empty")
        if self.due_when not in {"any", "all"}:
            raise ConfigError("maintenance due_when must be any or all")
        intervals = (self.interval_hours, self.interval_prints, self.interval_days)
        if not any(value is not None for value in intervals):
            raise ConfigError("maintenance task requires at least one interval")
        if any(value is not None and value <= 0 for value in intervals):
            raise ConfigError("maintenance intervals must be positive")
        if min(self.warning_hours, self.warning_prints, self.warning_days) < 0:
            raise ConfigError("maintenance warnings cannot be negative")
        return self


@dataclass(frozen=True, slots=True)
class PrinterObserverSettings:
    printer_id: str
    printer_model: str
    home_assistant_url: str
    entities: dict[str, str]
    poll_seconds: float = 15.0
    timeout_seconds: float = 3.0
    max_response_bytes: int = 4 * 1024 * 1024
    stale_after_seconds: int = 90
    permanent_sample_seconds: int = 300
    terminal_confirmations: int = 2
    database_path: Path = Path("/var/lib/home-sensor/printer.sqlite3")
    environment_location: str = "printer_room"
    baseline_minutes: int = 30
    recovery_minutes: int = 120
    firmware_version: str | None = None
    ams_units: tuple[AmsObserverSettings, ...] = ()
    maintenance_tasks: tuple[MaintenanceTaskSettings, ...] = ()
    monitoring_database_path: Path = Path("/var/lib/home-sensor/monitoring.sqlite3")
    monitoring_output_dir: Path = Path("/var/lib/home-sensor/exports")
    automatic_monitoring: bool = True
    cloud_history_enabled: bool = True
    cloud_history_refresh_seconds: int = 3600
    cloud_history_timeout_seconds: float = 15.0
    cloud_history_max_records: int = 1000
    manufacturer_maintenance_enabled: bool = True
    maintenance_evaluation_seconds: int = 300
    maintenance_rolling_window_days: int = 30
    maintenance_minimum_history_days: int = 7

    def validated(self) -> PrinterObserverSettings:
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", self.printer_id):
            raise ConfigError("printer_id must be a stable lowercase slug")
        if not self.printer_model.strip():
            raise ConfigError("printer_model cannot be empty")
        parsed = urlparse(self.home_assistant_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ConfigError("home_assistant_url must be an http(s) URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ConfigError(
                "plain HTTP Home Assistant access is restricted to loopback"
            )
        if set(self.entities) - KNOWN_ENTITY_KEYS:
            names = ", ".join(sorted(set(self.entities) - KNOWN_ENTITY_KEYS))
            raise ConfigError(f"unknown printer entity mapping keys: {names}")
        missing = {"online", "print_status"} - set(self.entities)
        if missing:
            raise ConfigError(
                f"printer entity mapping missing: {', '.join(sorted(missing))}"
            )
        invalid = [
            value
            for value in self.entities.values()
            if not ENTITY_ID_RE.fullmatch(value)
        ]
        if invalid:
            raise ConfigError(
                "printer entity mappings must be Home Assistant entity IDs"
            )
        if len(set(self.entities.values())) != len(self.entities):
            raise ConfigError("each printer field must map to a distinct HA entity")
        if not 5 <= self.poll_seconds <= 300:
            raise ConfigError("poll_seconds must be between 5 and 300")
        if not 0.5 <= self.timeout_seconds <= 15:
            raise ConfigError("timeout_seconds must be between 0.5 and 15")
        if not 4096 <= self.max_response_bytes <= 16 * 1024 * 1024:
            raise ConfigError("max_response_bytes is outside the safe range")
        if self.stale_after_seconds < self.poll_seconds * 2:
            raise ConfigError("stale_after_seconds must cover at least two polls")
        if not 60 <= self.permanent_sample_seconds <= 3600:
            raise ConfigError("permanent_sample_seconds must be between 60 and 3600")
        if not 1 <= self.terminal_confirmations <= 5:
            raise ConfigError("terminal_confirmations must be between 1 and 5")
        if not self.database_path.is_absolute():
            raise ConfigError("database_path must be absolute")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.environment_location):
            raise ConfigError("environment_location must be a stable slug")
        if not 5 <= self.baseline_minutes <= 180:
            raise ConfigError("baseline_minutes must be between 5 and 180")
        if not 15 <= self.recovery_minutes <= 1440:
            raise ConfigError("recovery_minutes must be between 15 and 1440")
        for unit in self.ams_units:
            unit.validated()
        if len({unit.ams_id for unit in self.ams_units}) != len(self.ams_units):
            raise ConfigError("AMS ids must be unique")
        for task in self.maintenance_tasks:
            task.validated()
        if len({task.task_id for task in self.maintenance_tasks}) != len(
            self.maintenance_tasks
        ):
            raise ConfigError("maintenance task ids must be unique")
        if not self.monitoring_database_path.is_absolute():
            raise ConfigError("monitoring database path must be absolute")
        if not self.monitoring_output_dir.is_absolute():
            raise ConfigError("monitoring output directory must be absolute")
        if not 300 <= self.cloud_history_refresh_seconds <= 86400:
            raise ConfigError("cloud history refresh must be between 300 and 86400")
        if not 1 <= self.cloud_history_timeout_seconds <= 30:
            raise ConfigError("cloud history timeout must be between 1 and 30")
        if not 1 <= self.cloud_history_max_records <= 5000:
            raise ConfigError("cloud history max records must be between 1 and 5000")
        if not 30 <= self.maintenance_evaluation_seconds <= 3600:
            raise ConfigError(
                "maintenance evaluation seconds must be between 30 and 3600"
            )
        if not 1 <= self.maintenance_rolling_window_days <= 365:
            raise ConfigError(
                "maintenance rolling window days must be between 1 and 365"
            )
        if not 0 <= self.maintenance_minimum_history_days <= 365:
            raise ConfigError(
                "maintenance minimum history days must be between 0 and 365"
            )
        if self.maintenance_minimum_history_days > self.maintenance_rolling_window_days:
            raise ConfigError(
                "maintenance minimum history days cannot exceed the rolling window"
            )
        return self


def load_printer_settings(path: Path) -> PrinterObserverSettings:
    try:
        with path.open("rb") as source:
            data = tomllib.load(source)
    except FileNotFoundError as exc:
        raise ConfigError(f"printer configuration not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load printer configuration: {exc}") from exc

    printer = _table(data, "printer")
    home_assistant = _table(data, "home_assistant")
    storage = _table(data, "storage")
    analysis = _table(data, "analysis")
    automatic_monitoring = _table(data, "automatic_monitoring")
    cloud_history = _table(data, "cloud_history")
    maintenance_engine = _table(data, "maintenance_engine")
    entities = _table(data, "entities")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in entities.items()
    ):
        raise ConfigError("[entities] values must be strings")
    ams_units = _ams_settings(data.get("ams", []))
    maintenance_tasks = _maintenance_settings(data.get("maintenance", []))
    firmware = str(printer.get("firmware_version", "")).strip() or None
    return PrinterObserverSettings(
        printer_id=str(printer.get("id", "x2d")),
        printer_model=str(printer.get("model", "X2D")),
        home_assistant_url=str(
            home_assistant.get("url", "http://127.0.0.1:8123")
        ).rstrip("/"),
        entities={str(key): str(value) for key, value in entities.items()},
        poll_seconds=float(home_assistant.get("poll_seconds", 15)),
        timeout_seconds=float(home_assistant.get("timeout_seconds", 3)),
        max_response_bytes=int(
            home_assistant.get("max_response_bytes", 4 * 1024 * 1024)
        ),
        stale_after_seconds=int(home_assistant.get("stale_after_seconds", 90)),
        permanent_sample_seconds=int(storage.get("permanent_sample_seconds", 300)),
        terminal_confirmations=int(storage.get("terminal_confirmations", 2)),
        database_path=Path(
            str(storage.get("database_path", "/var/lib/home-sensor/printer.sqlite3"))
        ).expanduser(),
        environment_location=str(analysis.get("environment_location", "printer_room")),
        baseline_minutes=int(analysis.get("baseline_minutes", 30)),
        recovery_minutes=int(analysis.get("recovery_minutes", 120)),
        firmware_version=firmware,
        ams_units=ams_units,
        maintenance_tasks=maintenance_tasks,
        monitoring_database_path=Path(
            str(
                automatic_monitoring.get(
                    "database_path", "/var/lib/home-sensor/monitoring.sqlite3"
                )
            )
        ).expanduser(),
        monitoring_output_dir=Path(
            str(automatic_monitoring.get("output_dir", "/var/lib/home-sensor/exports"))
        ).expanduser(),
        automatic_monitoring=bool(automatic_monitoring.get("enabled", True)),
        cloud_history_enabled=bool(cloud_history.get("enabled", True)),
        cloud_history_refresh_seconds=int(cloud_history.get("refresh_seconds", 3600)),
        cloud_history_timeout_seconds=float(cloud_history.get("timeout_seconds", 15)),
        cloud_history_max_records=int(cloud_history.get("max_records", 1000)),
        manufacturer_maintenance_enabled=bool(
            maintenance_engine.get("manufacturer_tasks_enabled", True)
        ),
        maintenance_evaluation_seconds=int(
            maintenance_engine.get("evaluation_seconds", 300)
        ),
        maintenance_rolling_window_days=int(
            maintenance_engine.get("rolling_window_days", 30)
        ),
        maintenance_minimum_history_days=int(
            maintenance_engine.get("minimum_history_days", 7)
        ),
    ).validated()


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _ams_settings(value: object) -> tuple[AmsObserverSettings, ...]:
    if not isinstance(value, list):
        raise ConfigError("[[ams]] must be an array of tables")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("[[ams]] entries must be tables")
        raw_entities = item.get("entities", {})
        trays = item.get("tray_entities", [])
        if not isinstance(raw_entities, dict) or not all(
            isinstance(key, str) and isinstance(entity, str)
            for key, entity in raw_entities.items()
        ):
            raise ConfigError("AMS entities must be string mappings")
        if not isinstance(trays, list) or not all(
            isinstance(entity, str) for entity in trays
        ):
            raise ConfigError("AMS tray_entities must be a string array")
        result.append(
            AmsObserverSettings(
                ams_id=str(item.get("id", "")),
                model=str(item.get("model", "")),
                entities={
                    str(key): str(entity) for key, entity in raw_entities.items()
                },
                tray_entities=tuple(trays),
            ).validated()
        )
    return tuple(result)


def _maintenance_settings(value: object) -> tuple[MaintenanceTaskSettings, ...]:
    if not isinstance(value, list):
        raise ConfigError("[[maintenance]] must be an array of tables")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ConfigError("[[maintenance]] entries must be tables")
        result.append(
            MaintenanceTaskSettings(
                task_id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                description=str(item.get("description", "")),
                interval_hours=_optional_float(item.get("interval_hours")),
                warning_hours=float(item.get("warning_hours", 0)),
                interval_prints=_optional_int(item.get("interval_prints")),
                warning_prints=int(item.get("warning_prints", 0)),
                interval_days=_optional_int(item.get("interval_days")),
                warning_days=int(item.get("warning_days", 0)),
                due_when=str(item.get("due_when", "any")),
                notes=str(item.get("notes", "")),
                source=str(item.get("source", "user_configured")),
                enabled=bool(item.get("enabled", True)),
            ).validated()
        )
    return tuple(result)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)
