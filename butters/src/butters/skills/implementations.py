"""Initial explicit read-only skills over typed integration adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from butters.integrations.model import (
    IntegrationError,
    PrintEnvironmentSnapshot,
    PrinterIntelligenceSnapshot,
    PrinterSnapshotProvider,
    SensorRecord,
    SensorSnapshot,
    SensorSnapshotProvider,
    ServerHealthProvider,
)
from butters.routing.entities import Entity, EntityRegistry, Metric, MetricRegistry
from butters.skills.model import (
    ActionClass,
    AirQualityArgs,
    AirQualityResult,
    ComparisonArgs,
    ComparisonMissing,
    ComparisonResult,
    CurrentPrintResult,
    EntityStatusResult,
    LastPrintResult,
    PrintEnvironmentResult,
    PrinterArgs,
    PrinterMaintenanceEventsResult,
    PrinterMaintenanceResult,
    PrinterStatusResult,
    PrinterTemperaturesResult,
    PrinterUsageResult,
    SensorLastSeenArgs,
    SensorLastSeenResult,
    SensorStatusArgs,
    SensorStatusResult,
    SensorValueArgs,
    SensorValueResult,
    SensorValuesArgs,
    SensorValuesResult,
    ServerHealthArgs,
    ServerHealthResult,
    SkillArguments,
    SkillError,
    SkillResult,
)
from butters.skills.policy import PolicyValidator, allow_arguments
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    optional_string,
    required_string,
    required_string_tuple,
    strict_arguments,
)

_SKILL_METADATA: dict[str, dict[str, object]] = {
    "get_sensor_value": {
        "category": "sensors",
        "input_schema": {
            "entity": "allow-listed entity ID",
            "metric": "allow-listed metric ID",
        },
        "result_description": "Current value, unit, reporting state, timestamp, and age.",
        "permission_summary": ("dashboard_api_read", "configured_entities_only"),
        "positive_examples": ("what is the humidity in box three", "printer room CO2"),
        "negative_examples": ("set the humidity", "read an unknown sensor"),
    },
    "get_sensor_values": {
        "category": "sensors",
        "input_schema": {
            "entity": "allow-listed entity ID",
            "metrics": "ordered list of allow-listed metric IDs",
        },
        "result_description": "One current value, unit, and availability per requested metric.",
        "permission_summary": ("dashboard_api_read", "configured_entities_only"),
        "positive_examples": ("what is the temperature and humidity in box three",),
        "negative_examples": ("set the humidity", "read an unknown sensor"),
    },
    "get_sensor_status": {
        "category": "sensors",
        "input_schema": {"entity": "optional allow-listed entity ID"},
        "result_description": "Bounded reporting state for one or all configured sensors.",
        "permission_summary": ("dashboard_api_read", "configured_entities_only"),
        "positive_examples": ("is every sensor reporting",),
        "negative_examples": ("restart the offline sensor",),
    },
    "get_sensor_last_seen": {
        "category": "sensors",
        "input_schema": {"entity": "allow-listed entity ID"},
        "result_description": "Latest receive time and age for one sensor.",
        "permission_summary": ("dashboard_api_read", "configured_entities_only"),
        "positive_examples": ("when was box two last seen",),
        "negative_examples": ("change its timestamp",),
    },
    "compare_sensor_metric": {
        "category": "sensors",
        "input_schema": {
            "group": "filament_boxes",
            "metric": "humidity",
            "operation": "max",
        },
        "result_description": "Deterministically calculated maximum and missing observations.",
        "permission_summary": ("dashboard_api_read", "local_computation"),
        "positive_examples": ("which filament box is most humid",),
        "negative_examples": ("change the driest box",),
    },
    "get_room_air_quality": {
        "category": "sensors",
        "input_schema": {"entity": "allow-listed air-quality entity ID"},
        "result_description": "Current bounded air-quality measurements and category.",
        "permission_summary": ("dashboard_api_read", "air_quality_entities_only"),
        "positive_examples": ("how is the printer room air quality",),
        "negative_examples": ("turn on an air purifier",),
    },
    "get_server_health": {
        "category": "system",
        "input_schema": {},
        "result_description": "Host resources and fixed allow-listed service states.",
        "permission_summary": (
            "procfs_read",
            "fixed_system_commands",
            "allowlisted_services",
        ),
        "positive_examples": ("what is the server status",),
        "negative_examples": ("run a command", "restart the server"),
    },
}


# get_printer_maintenance returns the whole bounded maintenance catalog, so it is
# structurally larger than any other read-only result and does not fit the 8192-byte
# SkillSpec default. The cap below is derived from the limits the maintenance
# adapter actually enforces, not from today's payload:
#
#   tasks               DashboardPrinterAdapter.maintenance keeps only
#                       MAINTENANCE_TASK_FIELDS (20 fields) per task, and tasks come
#                       solely from the fixed manufacturer catalog
#                       (server X2D_MAINTENANCE_TASKS: 11 entries, largest entry
#                       encodes to 825 bytes). Bounded here at 24 entries of 1024
#                       bytes, i.e. more than double the catalog with per-entry
#                       headroom.
#   completion_history  bounded by the adapter to completions[:20].
#   notifications       bounded by the adapter to recent_notifications[:20]; a
#                       MAINTENANCE_EVENT_FIELDS entry encodes to 551 bytes even
#                       when every field is a long identifier.
#   envelope            usage (USAGE_FIELDS encodes to 1559 bytes with long values),
#                       maintenance_summary, manufacturer_source, and dataclass keys.
#
# print_history is always empty on this path. The result therefore stays bounded and
# far below the 2 MiB integration response ceiling.
_MAINTENANCE_MAX_TASKS = 24
_MAINTENANCE_TASK_BYTES = 1024
_MAINTENANCE_HISTORY_ENTRIES = 20
_MAINTENANCE_HISTORY_ENTRY_BYTES = 512
_MAINTENANCE_EVENT_ENTRIES = 20
_MAINTENANCE_EVENT_ENTRY_BYTES = 640
_MAINTENANCE_ENVELOPE_BYTES = 4096
MAINTENANCE_MAX_RESULT_BYTES = (
    _MAINTENANCE_MAX_TASKS * _MAINTENANCE_TASK_BYTES
    + _MAINTENANCE_HISTORY_ENTRIES * _MAINTENANCE_HISTORY_ENTRY_BYTES
    + _MAINTENANCE_EVENT_ENTRIES * _MAINTENANCE_EVENT_ENTRY_BYTES
    + _MAINTENANCE_ENVELOPE_BYTES
)

# Explicit per-skill result budgets. Everything absent keeps the SkillSpec default.
_SKILL_RESULT_BYTES: dict[str, int] = {
    "get_printer_maintenance": MAINTENANCE_MAX_RESULT_BYTES,
}


def _skill(
    name: str,
    description: str,
    action_class: ActionClass,
    parser: object,
    authorizer: object,
    implementation: object,
    timeout_seconds: float,
) -> SkillSpec:
    metadata = dict(_SKILL_METADATA.get(name, {}))
    if name.startswith("get_printer") or name in {
        "get_current_print",
        "get_print_environment_summary",
        "get_last_print",
    }:
        metadata = {
            "category": "printer",
            "input_schema": {"entity": "configured printer entity ID"},
            "result_description": "Read-only observation from the bounded printer adapter.",
            "permission_summary": ("dashboard_api_read", "configured_printer_only"),
            "positive_examples": (description.removeprefix("Return ").rstrip("."),),
            "negative_examples": ("control the printer", "start or stop a print"),
        }
    if name in _SKILL_RESULT_BYTES:
        metadata["max_result_bytes"] = _SKILL_RESULT_BYTES[name]
    return SkillSpec(
        name,
        description,
        action_class,
        parser,  # type: ignore[arg-type]
        authorizer,  # type: ignore[arg-type]
        implementation,  # type: ignore[arg-type]
        timeout_seconds,
        source_reference="butters.skills.implementations",
        **metadata,
    )


class ReadOnlySkillImplementations:
    def __init__(
        self,
        sensor_provider: SensorSnapshotProvider,
        server_provider: ServerHealthProvider,
        entities: EntityRegistry,
        metrics: MetricRegistry,
        printer_provider: PrinterSnapshotProvider,
    ) -> None:
        self.sensor_provider = sensor_provider
        self.server_provider = server_provider
        self.entities = entities
        self.metrics = metrics
        self.printer_provider = printer_provider

    def authorize_value(self, arguments: SkillArguments) -> None:
        args = cast(SensorValueArgs, arguments)
        entity = self._allowed_entity(args.entity)
        metric = self._allowed_metric(args.metric)
        if entity.sensor_type not in metric.sensor_types:
            raise SkillError(
                "policy_denied",
                f"{entity.entity_id} does not allow metric {metric.metric_id}",
            )

    def authorize_values(self, arguments: SkillArguments) -> None:
        args = cast(SensorValuesArgs, arguments)
        entity = self._allowed_entity(args.entity)
        for metric_id in args.metrics:
            metric = self._allowed_metric(metric_id)
            if entity.sensor_type not in metric.sensor_types:
                raise SkillError(
                    "policy_denied",
                    f"{entity.entity_id} does not allow metric {metric.metric_id}",
                )

    def authorize_status(self, arguments: SkillArguments) -> None:
        args = cast(SensorStatusArgs, arguments)
        if args.entity is not None:
            entity = self._allowed_entity(args.entity)
            if entity.sensor_type == "printer":
                raise SkillError(
                    "policy_denied", "printer status requires a printer skill"
                )

    def authorize_last_seen(self, arguments: SkillArguments) -> None:
        args = cast(SensorLastSeenArgs, arguments)
        entity = self._allowed_entity(args.entity)
        if entity.sensor_type == "printer":
            raise SkillError(
                "policy_denied", "printer history requires a printer skill"
            )

    def authorize_comparison(self, arguments: SkillArguments) -> None:
        args = cast(ComparisonArgs, arguments)
        if args.group != "filament_boxes":
            raise SkillError("policy_denied", f"unsupported group: {args.group}")
        if args.metric != "humidity" or args.operation != "max":
            raise SkillError("policy_denied", "comparison is not allow-listed")

    def authorize_air_quality(self, arguments: SkillArguments) -> None:
        args = cast(AirQualityArgs, arguments)
        entity = self._allowed_entity(args.entity)
        if entity.sensor_type != "air_quality":
            raise SkillError("policy_denied", "entity is not an air-quality station")

    def authorize_printer(self, arguments: SkillArguments) -> None:
        args = cast(PrinterArgs, arguments)
        entity = self._allowed_entity(args.entity)
        if entity.sensor_type != "printer":
            raise SkillError("policy_denied", "entity is not a configured printer")

    def get_sensor_value(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorValueArgs, arguments)
        entity = self.entities.require(args.entity)
        metric = self.metrics.require(args.metric)
        record = self._record_or_missing(self.sensor_provider.snapshot(), entity)
        return self._measurement(entity, metric, record)

    def get_sensor_values(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorValuesArgs, arguments)
        entity = self.entities.require(args.entity)
        # One snapshot serves every requested metric, so the measurements in a
        # single answer are read from the same reported packet.
        record = self._record_or_missing(self.sensor_provider.snapshot(), entity)
        return SensorValuesResult(
            entity.entity_id,
            entity.display_name,
            record.status,
            tuple(
                self._measurement(entity, self.metrics.require(metric_id), record)
                for metric_id in args.metrics
            ),
        )

    def _measurement(
        self, entity: Entity, metric: Metric, record: SensorRecord
    ) -> SensorValueResult:
        raw = record.values.get(metric.field)
        battery_valid = not (
            metric.field == "battery_mv"
            and record.values.get("battery_measurement_ok") is False
        )
        available = (
            record.status == "online"
            and battery_valid
            and isinstance(raw, (int, float))
            and not isinstance(raw, bool)
        )
        reason = None
        value: float | int | None = None
        if available:
            scaled = raw * metric.scale
            value = (
                int(scaled) if metric.scale == 1.0 and isinstance(raw, int) else scaled
            )
        elif record.status != "online":
            reason = f"sensor is {record.status}"
        elif not battery_valid:
            reason = "battery measurement is not valid in the latest packet"
        else:
            reason = "measurement is unavailable"
        return SensorValueResult(
            entity.entity_id,
            entity.display_name,
            metric.metric_id,
            metric.display_name,
            value,
            metric.unit,
            record.last_seen,
            record.age_seconds,
            record.status,
            available,
            reason,
        )

    def get_sensor_status(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorStatusArgs, arguments)
        snapshot = self.sensor_provider.snapshot()
        selected = (
            (self.entities.require(args.entity),)
            if args.entity is not None
            else tuple(
                entity
                for entity in self.entities.entities
                if entity.sensor_type != "printer"
            )
        )
        statuses = tuple(
            self._entity_status(entity, self._record_or_missing(snapshot, entity))
            for entity in selected
        )
        reporting = sum(status.status == "online" for status in statuses)
        return SensorStatusResult(
            statuses,
            reporting == len(statuses),
            reporting,
            len(statuses),
        )

    def get_sensor_last_seen(self, arguments: SkillArguments) -> SkillResult:
        args = cast(SensorLastSeenArgs, arguments)
        entity = self.entities.require(args.entity)
        record = self._record_or_missing(self.sensor_provider.snapshot(), entity)
        return SensorLastSeenResult(
            entity.entity_id,
            entity.display_name,
            record.status,
            record.last_seen,
            record.age_seconds,
        )

    def compare_sensor_metric(self, arguments: SkillArguments) -> SkillResult:
        args = cast(ComparisonArgs, arguments)
        metric = self.metrics.require(args.metric)
        snapshot = self.sensor_provider.snapshot()
        available: list[tuple[float, Entity, SensorRecord]] = []
        missing: list[ComparisonMissing] = []
        for entity in self.entities.in_group(args.group):
            record = self._record_or_missing(snapshot, entity)
            raw = record.values.get(metric.field)
            if (
                record.status == "online"
                and isinstance(raw, (int, float))
                and not isinstance(raw, bool)
            ):
                available.append((float(raw) * metric.scale, entity, record))
            else:
                reason = (
                    f"sensor is {record.status}"
                    if record.status != "online"
                    else "measurement is unavailable"
                )
                missing.append(
                    ComparisonMissing(
                        entity.entity_id, entity.display_name, record.status, reason
                    )
                )
        if not available:
            return ComparisonResult(
                args.group,
                args.metric,
                args.operation,
                None,
                None,
                None,
                metric.unit,
                None,
                None,
                0,
                tuple(missing),
            )
        value, entity, record = max(available, key=lambda item: item[0])
        return ComparisonResult(
            args.group,
            args.metric,
            args.operation,
            entity.entity_id,
            entity.display_name,
            value,
            metric.unit,
            record.last_seen,
            record.age_seconds,
            len(available),
            tuple(missing),
        )

    def get_room_air_quality(self, arguments: SkillArguments) -> SkillResult:
        args = cast(AirQualityArgs, arguments)
        entity = self.entities.require(args.entity)
        record = self._record_or_missing(self.sensor_provider.snapshot(), entity)
        overall = record.values.get("overall_status")
        overall = overall if isinstance(overall, Mapping) else {}
        measurements: dict[str, float | int | None] = {}
        for metric_id in (
            "co2",
            "pm25",
            "pm10",
            "voc_index",
            "nox_index",
            "temperature",
            "humidity",
        ):
            metric = self.metrics.require(metric_id)
            value = record.values.get(metric.field)
            measurements[metric_id] = (
                value
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else None
            )
        return AirQualityResult(
            entity.entity_id,
            entity.display_name,
            record.status,
            record.last_seen,
            record.age_seconds,
            measurements,
            _optional_string(overall.get("category")),
            _optional_string(overall.get("severity")),
            _optional_string(overall.get("driving_metric")),
        )

    def get_server_health(self, _arguments: SkillArguments) -> SkillResult:
        return ServerHealthResult(self.server_provider.snapshot())

    def get_printer_status(self, arguments: SkillArguments) -> SkillResult:
        return PrinterStatusResult(self._printer(arguments))

    def get_current_print(self, arguments: SkillArguments) -> SkillResult:
        return CurrentPrintResult(self._printer(arguments))

    def get_printer_temperatures(self, arguments: SkillArguments) -> SkillResult:
        return PrinterTemperaturesResult(self._printer(arguments))

    def get_print_environment_summary(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        return PrintEnvironmentResult(self.printer_provider.environment_summary())

    def get_printer_usage(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        method = getattr(self.printer_provider, "usage", None)
        if callable(method):
            usage = method()
            return PrinterUsageResult(PrinterIntelligenceSnapshot(usage, (), (), ()))
        return PrinterUsageResult(self.printer_provider.intelligence())

    def get_printer_maintenance(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        method = getattr(self.printer_provider, "maintenance", None)
        if callable(method):
            return PrinterMaintenanceResult(method())
        return PrinterMaintenanceResult(self.printer_provider.intelligence())

    def get_printer_maintenance_events(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        method = getattr(self.printer_provider, "maintenance_events", None)
        if callable(method):
            return PrinterMaintenanceEventsResult(method(20))
        return PrinterMaintenanceEventsResult(
            self.printer_provider.intelligence().maintenance_notifications
        )

    def get_last_print(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        return LastPrintResult(self.printer_provider.intelligence())

    def _printer(self, arguments: SkillArguments):
        entity = self._printer_entity(arguments)
        snapshot = self.printer_provider.current()
        if snapshot.printer_id != entity.source_id:
            raise IntegrationError(
                "unavailable", "configured printer state is unavailable"
            )
        return snapshot

    def _printer_entity(self, arguments: SkillArguments) -> Entity:
        args = cast(PrinterArgs, arguments)
        return self.entities.require(args.entity)

    def _allowed_entity(self, entity_id: str) -> Entity:
        entity = self.entities.get(entity_id)
        if entity is None:
            raise SkillError("policy_denied", f"unsupported entity: {entity_id}")
        return entity

    def _allowed_metric(self, metric_id: str) -> Metric:
        metric = self.metrics.get(metric_id)
        if metric is None:
            raise SkillError("policy_denied", f"unsupported metric: {metric_id}")
        return metric

    @staticmethod
    def _record_or_missing(snapshot: SensorSnapshot, entity: Entity) -> SensorRecord:
        record = snapshot.find(entity.sensor_type, entity.source_id)
        return record or SensorRecord(
            entity.sensor_type,
            entity.source_id,
            None,
            None,
            "offline",
            {},
        )

    @staticmethod
    def _entity_status(entity: Entity, record: SensorRecord) -> EntityStatusResult:
        return EntityStatusResult(
            entity.entity_id,
            entity.display_name,
            record.status,
            record.last_seen,
            record.age_seconds,
        )


def build_read_only_registry(
    sensor_provider: SensorSnapshotProvider,
    server_provider: ServerHealthProvider,
    entities: EntityRegistry,
    metrics: MetricRegistry,
    printer_provider: PrinterSnapshotProvider | None = None,
    policy: PolicyValidator | None = None,
) -> SkillRegistry:
    printer_provider = printer_provider or _UnavailablePrinterProvider()
    implementation = ReadOnlySkillImplementations(
        sensor_provider, server_provider, entities, metrics, printer_provider
    )
    registry = SkillRegistry(policy)
    registry.register(
        _skill(
            "get_sensor_value",
            "Return one current allow-listed sensor measurement.",
            ActionClass.READ_ONLY,
            _parse_sensor_value,
            implementation.authorize_value,
            implementation.get_sensor_value,
            5.0,
        )
    )
    registry.register(
        _skill(
            "get_sensor_values",
            "Return several current allow-listed measurements from one sensor.",
            ActionClass.READ_ONLY,
            _parse_sensor_values,
            implementation.authorize_values,
            implementation.get_sensor_values,
            5.0,
        )
    )
    registry.register(
        _skill(
            "get_sensor_status",
            "Return reporting status for one or all configured sensors.",
            ActionClass.READ_ONLY,
            _parse_sensor_status,
            implementation.authorize_status,
            implementation.get_sensor_status,
            5.0,
        )
    )
    registry.register(
        _skill(
            "get_sensor_last_seen",
            "Return the latest timestamp and age for one configured sensor.",
            ActionClass.READ_ONLY,
            _parse_sensor_last_seen,
            implementation.authorize_last_seen,
            implementation.get_sensor_last_seen,
            5.0,
        )
    )
    registry.register(
        _skill(
            "compare_sensor_metric",
            "Compare current values across one allow-listed sensor group.",
            ActionClass.READ_ONLY,
            _parse_comparison,
            implementation.authorize_comparison,
            implementation.compare_sensor_metric,
            5.0,
        )
    )
    registry.register(
        _skill(
            "get_room_air_quality",
            "Return a concise structured room air-quality snapshot.",
            ActionClass.READ_ONLY,
            _parse_air_quality,
            implementation.authorize_air_quality,
            implementation.get_room_air_quality,
            5.0,
        )
    )
    registry.register(
        _skill(
            "get_server_health",
            "Return local host resources and fixed allow-listed service status.",
            ActionClass.READ_ONLY,
            _parse_server_health,
            allow_arguments,
            implementation.get_server_health,
            5.0,
        )
    )
    for name, description, method in (
        (
            "get_printer_status",
            "Return read-only state for one configured printer.",
            implementation.get_printer_status,
        ),
        (
            "get_current_print",
            "Return current print job, progress, remaining time, layer, and material.",
            implementation.get_current_print,
        ),
        (
            "get_printer_temperatures",
            "Return observed nozzle, bed, and chamber temperatures.",
            implementation.get_printer_temperatures,
        ),
        (
            "get_print_environment_summary",
            "Return an observational SEN66 summary for the latest print session.",
            implementation.get_print_environment_summary,
        ),
        (
            "get_printer_usage",
            "Return read-only local and upstream printer usage provenance and print counts.",
            implementation.get_printer_usage,
        ),
        (
            "get_printer_maintenance",
            "Return read-only local printer maintenance due state and completion history.",
            implementation.get_printer_maintenance,
        ),
        (
            "get_printer_maintenance_events",
            "Return up to twenty recent printer maintenance transition events.",
            implementation.get_printer_maintenance_events,
        ),
        (
            "get_last_print",
            "Return read-only metadata for the latest local or cloud-history print.",
            implementation.get_last_print,
        ),
    ):
        registry.register(
            _skill(
                name,
                description,
                ActionClass.READ_ONLY,
                _parse_printer,
                implementation.authorize_printer,
                method,
                5.0,
            )
        )
    return registry


def _parse_sensor_value(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity", "metric"}))
    return SensorValueArgs(
        required_string(values, "entity"), required_string(values, "metric")
    )


def _parse_sensor_values(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity", "metrics"}))
    return SensorValuesArgs(
        required_string(values, "entity"), required_string_tuple(values, "metrics")
    )


def _parse_sensor_status(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, optional=frozenset({"entity"}))
    return SensorStatusArgs(optional_string(values, "entity"))


def _parse_sensor_last_seen(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity"}))
    return SensorLastSeenArgs(required_string(values, "entity"))


def _parse_comparison(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"group", "metric", "operation"}))
    return ComparisonArgs(
        required_string(values, "group"),
        required_string(values, "metric"),
        required_string(values, "operation"),
    )


def _parse_air_quality(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity"}))
    return AirQualityArgs(required_string(values, "entity"))


def _parse_server_health(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values)
    return ServerHealthArgs()


def _parse_printer(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"entity"}))
    return PrinterArgs(required_string(values, "entity"))


class _UnavailablePrinterProvider:
    def current(self):
        raise IntegrationError("unavailable", "printer observer is unavailable")

    def environment_summary(self) -> PrintEnvironmentSnapshot:
        raise IntegrationError("unavailable", "printer observer is unavailable")

    def intelligence(self):
        raise IntegrationError("unavailable", "printer observer is unavailable")


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
