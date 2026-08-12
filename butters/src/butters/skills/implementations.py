"""Initial explicit read-only skills over typed integration adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from butters.integrations.model import (
    IntegrationError,
    PrintEnvironmentSnapshot,
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
    ServerHealthArgs,
    ServerHealthResult,
    SkillArguments,
    SkillError,
    SkillResult,
)
from butters.skills.policy import allow_arguments
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    optional_string,
    required_string,
    strict_arguments,
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
        return PrinterUsageResult(self.printer_provider.intelligence())

    def get_printer_maintenance(self, arguments: SkillArguments) -> SkillResult:
        self._printer_entity(arguments)
        return PrinterMaintenanceResult(self.printer_provider.intelligence())

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
) -> SkillRegistry:
    printer_provider = printer_provider or _UnavailablePrinterProvider()
    implementation = ReadOnlySkillImplementations(
        sensor_provider, server_provider, entities, metrics, printer_provider
    )
    registry = SkillRegistry()
    registry.register(
        SkillSpec(
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
        SkillSpec(
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
        SkillSpec(
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
        SkillSpec(
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
        SkillSpec(
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
        SkillSpec(
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
            "get_last_print",
            "Return read-only metadata for the latest local or cloud-history print.",
            implementation.get_last_print,
        ),
    ):
        registry.register(
            SkillSpec(
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
