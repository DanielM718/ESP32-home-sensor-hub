"""Concise response text kept separate from routing and data access."""

from __future__ import annotations

from butters.skills.model import (
    AirQualityResult,
    ComparisonResult,
    SensorLastSeenResult,
    SensorStatusResult,
    SensorValueResult,
    ServerHealthResult,
    SkillExecution,
)


class ResponseFormatter:
    def format_execution(self, execution: SkillExecution) -> str:
        if execution.failure is not None:
            return self._failure(execution.failure.code, execution.failure.message)
        result = execution.result
        if isinstance(result, SensorValueResult):
            return self._sensor_value(result)
        if isinstance(result, SensorStatusResult):
            return self._sensor_status(result)
        if isinstance(result, SensorLastSeenResult):
            return self._last_seen(result)
        if isinstance(result, ComparisonResult):
            return self._comparison(result)
        if isinstance(result, AirQualityResult):
            return self._air_quality(result)
        if isinstance(result, ServerHealthResult):
            return self._server_health(result)
        return "The read-only skill returned no usable result."

    @staticmethod
    def _sensor_value(result: SensorValueResult) -> str:
        if not result.available or result.value is None:
            reason = result.reason or "the measurement is unavailable"
            return (
                f"{result.display_name} {result.metric_name} is unavailable: {reason}."
            )
        value = _format_value(result.metric, result.value)
        unit = _spoken_unit(result.unit)
        suffix = f" {unit}" if unit else ""
        return f"{result.display_name} {result.metric_name} is {value}{suffix}."

    @staticmethod
    def _sensor_status(result: SensorStatusResult) -> str:
        if result.configured_count == 1:
            item = result.entities[0]
            if item.status == "online":
                return f"{item.display_name} is reporting; last seen {_age_phrase(item.age_seconds)}."
            return f"{item.display_name} is {item.status}; last seen {_age_phrase(item.age_seconds)}."
        if result.all_reporting:
            return f"All {result.configured_count} configured sensors are reporting."
        missing = [
            f"{item.display_name} is {item.status}"
            for item in result.entities
            if item.status != "online"
        ]
        details = "; ".join(missing)
        return (
            f"{result.reporting_count} of {result.configured_count} configured sensors "
            f"are reporting. {details}."
        )

    @staticmethod
    def _last_seen(result: SensorLastSeenResult) -> str:
        if result.timestamp is None:
            return f"{result.display_name} has no valid last-seen timestamp."
        return (
            f"{result.display_name} was last seen {_age_phrase(result.age_seconds)} "
            f"and is {result.status}."
        )

    @staticmethod
    def _comparison(result: ComparisonResult) -> str:
        if result.entity is None or result.value is None:
            return "No currently reporting filament box has a humidity value."
        text = (
            f"{result.display_name} is the most humid at "
            f"{_format_value(result.metric, result.value)} "
            f"{_spoken_unit(result.unit)}."
        )
        if result.missing:
            names = ", ".join(item.display_name for item in result.missing)
            text += f" Excluded unavailable data from {names}."
        return text

    @staticmethod
    def _air_quality(result: AirQualityResult) -> str:
        if result.status != "online":
            return (
                f"{result.display_name} air-quality data is {result.status}; "
                f"last seen {_age_phrase(result.age_seconds)}."
            )
        values = result.measurements
        parts = []
        if values.get("co2") is not None:
            parts.append(f"CO2 {round(float(values['co2']))} ppm")
        if values.get("pm25") is not None:
            parts.append(
                f"PM2.5 {_format_decimal(values['pm25'], 1)} micrograms per cubic meter"
            )
        if values.get("voc_index") is not None:
            parts.append(f"VOC index {round(float(values['voc_index']))}")
        if values.get("nox_index") is not None:
            parts.append(f"NOx index {round(float(values['nox_index']))}")
        measurements = ", ".join(parts)
        if not measurements:
            return f"{result.display_name} has no current air-quality measurements."
        category = result.summary_category
        if category:
            return (
                f"{result.display_name} dashboard summary: {category}. "
                f"Current readings are {measurements}."
            )
        return f"{result.display_name} current readings are {measurements}."

    @staticmethod
    def _server_health(result: ServerHealthResult) -> str:
        health = result.health
        active = sum(service.active for service in health.services)
        total = len(health.services)
        inactive = [service.name for service in health.services if not service.active]
        available = (
            "unknown"
            if health.available_memory_bytes is None
            else f"{health.available_memory_bytes / (1024**3):.1f} gigabytes"
        )
        load_description = (
            "low"
            if health.load_1m < 1.0
            else "moderate"
            if health.load_1m < 3.0
            else "high"
        )
        text = (
            f"Server load is {load_description} at {health.load_1m:.2f}. "
            f"About {available} of RAM is available. "
            f"{active} of {total} allow-listed services are active."
        )
        if inactive:
            text += f" Not active: {', '.join(inactive)}."
        return text

    @staticmethod
    def _failure(code: str, message: str) -> str:
        if code in {"timeout", "unavailable", "upstream_status"}:
            return "Current home-sensor data is temporarily unavailable."
        if code in {"unknown_skill", "policy_denied", "invalid_arguments"}:
            return "That request is not permitted by the read-only skill policy."
        if code == "sensor_unavailable":
            return message.rstrip(".") + "."
        return "The read-only request could not be completed."


def _format_value(metric: str, value: float) -> str:
    if metric in {"co2", "voc_index", "nox_index"}:
        return str(round(float(value)))
    if metric == "battery_voltage":
        return f"{float(value):.3f}".rstrip("0").rstrip(".")
    if metric == "temperature":
        return _format_decimal(value, 1)
    if metric == "humidity":
        return str(round(float(value)))
    return _format_decimal(value, 1)


def _format_decimal(value: float, places: int) -> str:
    return f"{float(value):.{places}f}".rstrip("0").rstrip(".")


def _spoken_unit(unit: str) -> str:
    if unit == "%":
        return "percent"
    if unit == "°C":
        return "degrees Celsius"
    if unit == "µg/m³":
        return "micrograms per cubic meter"
    if unit == "V":
        return "volts"
    if unit == "index":
        return ""
    return unit


def _age_phrase(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "at an unknown time"
    if age_seconds < 10:
        return "just now"
    if age_seconds < 60:
        return f"{age_seconds} seconds ago"
    minutes = round(age_seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = round(age_seconds / 3600)
    if hours < 48:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = round(age_seconds / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
