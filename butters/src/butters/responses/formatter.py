"""Concise response text kept separate from routing and data access."""

from __future__ import annotations

from butters.skills.model import (
    AirQualityResult,
    ComparisonResult,
    CurrentPrintResult,
    LastPrintResult,
    PrintEnvironmentResult,
    PrinterMaintenanceResult,
    PrinterStatusResult,
    PrinterTemperaturesResult,
    PrinterUsageResult,
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
        if isinstance(result, PrinterStatusResult):
            return self._printer_status(result)
        if isinstance(result, CurrentPrintResult):
            return self._current_print(result)
        if isinstance(result, PrinterTemperaturesResult):
            return self._printer_temperatures(result)
        if isinstance(result, PrintEnvironmentResult):
            return self._print_environment(result)
        if isinstance(result, PrinterUsageResult):
            return self._printer_usage(result)
        if isinstance(result, PrinterMaintenanceResult):
            return self._printer_maintenance(result)
        if isinstance(result, LastPrintResult):
            return self._last_print(result)
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
    def _printer_status(result: PrinterStatusResult) -> str:
        printer = result.printer
        name = printer.printer_model
        if not printer.online:
            return f"{name} is offline."
        state = printer.normalized_state.replace("_", " ")
        text = f"{name} is {state}."
        progress = _number(printer.values.get("progress_percent"))
        if (
            state in {"preparing", "printing", "paused", "finishing"}
            and progress is not None
        ):
            text += f" Progress is {_format_decimal(progress, 1)} percent."
        return text

    @staticmethod
    def _current_print(result: CurrentPrintResult) -> str:
        printer = result.printer
        if not printer.online:
            return f"{printer.printer_model} is offline, so the current print is unavailable."
        if printer.normalized_state not in {
            "preparing",
            "printing",
            "paused",
            "finishing",
        }:
            return f"{printer.printer_model} has no active print."
        values = printer.values
        job = _text(values.get("job_name")) or "an unnamed job"
        parts = [
            f"{printer.printer_model} is {printer.normalized_state}, working on {job}"
        ]
        progress = _number(values.get("progress_percent"))
        if progress is not None:
            parts.append(f"{_format_decimal(progress, 1)} percent complete")
        remaining = _integer(values.get("remaining_seconds"))
        if remaining is not None:
            parts.append(f"about {_duration(remaining)} remaining")
        current_layer = _integer(values.get("current_layer"))
        total_layers = _integer(values.get("total_layers"))
        if current_layer is not None and total_layers is not None:
            parts.append(f"layer {current_layer} of {total_layers}")
        elif current_layer is not None:
            parts.append(f"layer {current_layer}")
        material = _text(values.get("active_material"))
        if material is not None:
            provenance = printer.provenance.get("active_material", "unknown")
            label = {
                "observed": "observed material",
                "inferred_active_ams_tray": "material inferred from the active AMS tray",
            }.get(provenance, "material with unknown provenance")
            parts.append(f"{label} {material}")
        return "; ".join(parts) + "."

    @staticmethod
    def _printer_temperatures(result: PrinterTemperaturesResult) -> str:
        printer = result.printer
        if not printer.online:
            return (
                f"{printer.printer_model} is offline, so temperatures are unavailable."
            )
        values = printer.values
        labels = (
            ("nozzle one", "nozzle_1_temperature", "nozzle_1_target"),
            ("nozzle two", "nozzle_2_temperature", "nozzle_2_target"),
            ("bed", "bed_temperature", "bed_target"),
            ("chamber", "chamber_temperature", None),
        )
        parts = []
        for label, current_key, target_key in labels:
            current = _number(values.get(current_key))
            if current is None:
                continue
            text = f"{label} {_format_decimal(current, 1)} degrees Celsius"
            target = _number(values.get(target_key)) if target_key is not None else None
            if target is not None:
                text += f", target {_format_decimal(target, 1)}"
            parts.append(text)
        if not parts:
            return f"{printer.printer_model} has no observed temperature values."
        return f"{printer.printer_model}: " + "; ".join(parts) + "."

    @staticmethod
    def _print_environment(result: PrintEnvironmentResult) -> str:
        summary = result.summary
        if not summary.available:
            reason = (summary.reason or "summary unavailable").replace("_", " ")
            return f"The last-print environmental summary is unavailable: {reason}."
        job = _text(summary.session.get("job_name")) or "the last print"
        parts = []
        pm25 = summary.metrics.get("pm25", {})
        peak = _number(pm25.get("print_peak"))
        if peak is not None:
            parts.append(
                f"peak PM2.5 was {_format_decimal(peak, 1)} micrograms per cubic meter"
            )
        voc = summary.metrics.get("voc_index", {})
        voc_change = _number(voc.get("change_from_baseline"))
        if voc_change is not None:
            direction = "increased" if voc_change >= 0 else "decreased"
            parts.append(
                f"VOC index {direction} by {_format_decimal(abs(voc_change), 1)} from baseline during the print"
            )
        if summary.voc_recovery_seconds is not None:
            parts.append(
                f"VOC returned to the configured baseline range after about {_duration(summary.voc_recovery_seconds)}"
            )
        if not parts:
            return f"Environmental samples exist for {job}, but the requested summary values are unavailable."
        return (
            f"For {job}, "
            + "; ".join(parts)
            + ". This is an observational association, not proof of causation."
        )

    @staticmethod
    def _printer_usage(result: PrinterUsageResult) -> str:
        usage = result.intelligence.usage
        local_hours = _number(usage.get("locally_observed_print_hours")) or 0.0
        completed = _integer(usage.get("locally_observed_completed_print_count")) or 0
        effective = _number(usage.get("maintenance_effective_lifetime_hours"))
        text = (
            f"The printer has {local_hours:.1f} locally observed print hours and "
            f"{completed} locally observed completed prints."
        )
        reported = _number(usage.get("printer_reported_lifetime_hours"))
        if reported is None:
            text += " The X2D integration does not expose a printer-reported lifetime counter."
        else:
            text += f" The printer-reported lifetime value is {reported:.1f} hours."
        if effective is not None:
            text += f" The local maintenance position is {effective:.1f} hours."
        return text

    @staticmethod
    def _printer_maintenance(result: PrinterMaintenanceResult) -> str:
        enabled = [
            task
            for task in result.intelligence.maintenance_tasks
            if task.get("enabled") is True
        ]
        overdue = [
            str(task.get("name")) for task in enabled if task.get("overdue") is True
        ]
        warning = [
            str(task.get("name")) for task in enabled if task.get("warning") is True
        ]
        if overdue:
            text = f"Overdue printer maintenance: {', '.join(overdue)}."
        elif warning:
            text = (
                f"Printer maintenance approaching its reminder: {', '.join(warning)}."
            )
        elif enabled:
            text = f"All {len(enabled)} configured printer maintenance tasks are currently within their local intervals."
        else:
            text = "No printer maintenance tasks are configured."
        completions = result.intelligence.completion_history
        if completions:
            latest = completions[0]
            text += f" The last recorded service was {latest.get('completed_at', 'at an unknown time')}."
        return text

    @staticmethod
    def _last_print(result: LastPrintResult) -> str:
        if not result.intelligence.print_history:
            return "No local or imported printer history is available."
        item = result.intelligence.print_history[0]
        job = _text(item.get("job_name")) or "an unnamed job"
        outcome = _text(item.get("result")) or "unknown"
        duration = _integer(item.get("duration_seconds"))
        duration_text = (
            f" It lasted {_duration(duration)}."
            if duration is not None
            else " Its duration is unknown."
        )
        source = (
            "Bambu Cloud history"
            if item.get("source") == "bambu_cloud_history"
            else "local observation"
        )
        return f"The latest print was {job}; result {outcome}, from {source}.{duration_text}"

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


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _integer(value: object) -> int | None:
    number = _number(value)
    return None if number is None else round(number)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, remainder = divmod(minutes, 60)
    if remainder:
        return f"{hours} hour{'s' if hours != 1 else ''} {remainder} minutes"
    return f"{hours} hour{'s' if hours != 1 else ''}"
