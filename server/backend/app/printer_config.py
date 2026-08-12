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
        "nozzle_1_temperature",
        "nozzle_1_target",
        "nozzle_2_temperature",
        "nozzle_2_target",
        "bed_temperature",
        "bed_target",
        "chamber_temperature",
        "active_tool",
        "active_material",
        "active_filament",
        "ams_state",
        "ams_slot",
        "print_source",
        "firmware_version",
    }
)


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
    entities = _table(data, "entities")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in entities.items()
    ):
        raise ConfigError("[entities] values must be strings")
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
    ).validated()


def _table(data: dict[str, object], name: str) -> dict[str, object]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value
