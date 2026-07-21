"""Aligned SEN66 aggregation, de-duplication, and pollution event state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable, Mapping

from app.air_quality_policy import (
    EVENT_RULES,
    NOX_RELIABLE_EVENT_SECONDS,
    SENSOR_INVALID_EVENT_RULE,
    VOC_RELIABLE_EVENT_SECONDS,
    EventRule,
)
from app.models import AirQualityReading


WINDOW_SECONDS = 15 * 60
MEAN_MAX_METRICS = ("co2", "pm1", "pm25", "pm4", "pm10", "nox_index")
MEAN_ONLY_METRICS = ("temperature_c", "humidity")
MEAN_MIN_MAX_METRICS = ("voc_index", "sraw_voc", "sraw_nox")
P95_METRICS = ("co2", "pm25", "pm10", "voc_index", "nox_index")
ALL_METRICS = MEAN_MAX_METRICS + MEAN_ONLY_METRICS + MEAN_MIN_MAX_METRICS


@dataclass(frozen=True)
class PointData:
    measurement: str
    tags: Mapping[str, str]
    fields: Mapping[str, float | int | bool | str]
    timestamp: datetime


@dataclass(frozen=True)
class PipelineResult:
    accepted: bool
    duplicate: bool = False
    late: bool = False
    aggregate_points: tuple[PointData, ...] = ()
    event_points: tuple[PointData, ...] = ()


@dataclass
class WindowAccumulator:
    location: str
    topic: str
    node_id: int | None
    window_start: datetime
    window_end: datetime
    sample_count: int = 0
    valid_sample_count: int = 0
    invalid_sample_count: int = 0
    values: dict[str, list[float]] = field(
        default_factory=lambda: {metric: [] for metric in ALL_METRICS}
    )

    def add(self, reading: AirQualityReading) -> None:
        self.sample_count += 1
        if reading.sample_valid:
            self.valid_sample_count += 1
            for metric in ALL_METRICS:
                value = getattr(reading, metric)
                if _finite(value):
                    self.values[metric].append(float(value))
        else:
            self.invalid_sample_count += 1

    def point(
        self,
        *,
        expected_publish_seconds: int,
        partial: bool,
        observed_until: datetime | None = None,
    ) -> PointData:
        if partial:
            until = min(observed_until or _utc_now(), self.window_end)
            elapsed = max(0.0, (until - self.window_start).total_seconds())
            expected = min(
                WINDOW_SECONDS // expected_publish_seconds,
                max(1, math.floor(elapsed / expected_publish_seconds) + 1),
            )
        else:
            expected = WINDOW_SECONDS // expected_publish_seconds

        fields: dict[str, float | int | bool | str] = {
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "sample_count": self.sample_count,
            "valid_sample_count": self.valid_sample_count,
            "invalid_sample_count": self.invalid_sample_count,
            "expected_sample_count": expected,
            "data_coverage": round(
                min(100.0, self.valid_sample_count / expected * 100.0), 2
            ),
            "is_partial": partial,
        }
        for metric in MEAN_MAX_METRICS:
            _add_stats(fields, metric, self.values[metric], mean=True, maximum=True)
        for metric in MEAN_ONLY_METRICS:
            _add_stats(fields, metric, self.values[metric], mean=True)
        for metric in MEAN_MIN_MAX_METRICS:
            _add_stats(
                fields, metric, self.values[metric], mean=True, minimum=True, maximum=True
            )
        for metric in P95_METRICS:
            if self.values[metric]:
                fields[f"{metric}_p95"] = round(_percentile(self.values[metric], 95), 4)

        tags = {
            "location": self.location,
            "topic": self.topic,
            "sensor_type": "air_quality",
        }
        if self.node_id is not None:
            tags["node_id"] = str(self.node_id)
        return PointData("air_quality_15m", tags, fields, self.window_start)

    def means(self) -> dict[str, float]:
        return {
            metric: sum(values) / len(values)
            for metric, values in self.values.items()
            if values
        }


@dataclass
class ActiveEvent:
    rule: EventRule
    location: str
    topic: str
    node_id: int | None
    start: datetime
    trigger_value: float
    peak_value: float
    baseline_value: float | None
    preceding_window_value: float | None
    sample_count: int = 1
    last_observed: datetime | None = None


class EventDetector:
    """Stateful crossing detector with hysteresis, peak tracking, and cooldown."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], ActiveEvent] = {}
        self._cooldown_until: dict[tuple[str, str], datetime] = {}
        self._previous_values: dict[tuple[str, str], float] = {}

    def restore(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Restore permanent active-event records before live-sample replay."""

        rules = {
            rule.event_type: rule
            for rule in (*EVENT_RULES, SENSOR_INVALID_EVENT_RULE)
        }
        restored = 0
        for row in rows:
            event_type = str(row.get("event_type") or "")
            rule = rules.get(event_type)
            location = str(row.get("location") or "")
            start = _parse_datetime(row.get("event_start") or row.get("_time"))
            trigger_value = _number(row.get("trigger_value"))
            if rule is None or not location or start is None or trigger_value is None:
                continue
            key = (location, event_type)
            existing = self._active.get(key)
            if existing is not None and existing.start >= start:
                continue
            self._active[key] = ActiveEvent(
                rule=rule,
                location=location,
                topic=str(row.get("topic") or f"home/air/{location}"),
                node_id=_optional_int(row.get("node_id")),
                start=start,
                trigger_value=trigger_value,
                peak_value=_number(row.get("peak_value")) or trigger_value,
                baseline_value=_number(row.get("baseline_value")),
                preceding_window_value=_number(row.get("preceding_window_value")),
                sample_count=max(1, _optional_int(row.get("sample_count")) or 1),
                last_observed=(
                    _parse_datetime(row.get("last_observed")) or start
                ),
            )
            restored += 1
        return restored

    def process(
        self,
        reading: AirQualityReading,
        previous_window_means: Mapping[str, float],
    ) -> list[PointData]:
        points: list[PointData] = []
        if not reading.sample_valid:
            points.extend(self._process_invalid(reading))
            return points
        points.extend(self._clear_invalid(reading))

        current_values: dict[str, float] = {}
        for rule in EVENT_RULES:
            raw = getattr(reading, rule.metric)
            if not _finite(raw) or not self._gas_event_is_ready(reading, rule.metric):
                continue
            value = float(raw)
            current_values[rule.metric] = value
            previous_key = (reading.location, rule.metric)
            baseline = self._previous_values.get(previous_key)
            evaluated = value if rule.baseline_mode == "threshold" else (
                max(0.0, value - baseline) if baseline is not None else 0.0
            )
            key = (reading.location, rule.event_type)
            active = self._active.get(key)

            # Recovery can replay samples from before an event that was
            # restored from permanent storage. They may seed the delta
            # baseline, but must not update or clear the later event.
            if (
                active is not None
                and reading.received_at <= (active.last_observed or active.start)
            ):
                continue

            if active is None:
                cooldown_until = self._cooldown_until.get(key)
                if (
                    evaluated >= rule.trigger
                    and (cooldown_until is None or reading.received_at >= cooldown_until)
                ):
                    active = ActiveEvent(
                        rule=rule,
                        location=reading.location,
                        topic=reading.topic,
                        node_id=reading.node_id,
                        start=reading.received_at,
                        trigger_value=value,
                        peak_value=value,
                        baseline_value=baseline,
                        preceding_window_value=previous_window_means.get(rule.metric),
                        last_observed=reading.received_at,
                    )
                    self._active[key] = active
                    points.append(_event_point(active, reading.received_at, "active"))
            else:
                active.sample_count += 1
                active.peak_value = max(active.peak_value, value)
                active.last_observed = reading.received_at
                if evaluated <= rule.clear:
                    points.append(_event_point(active, reading.received_at, "completed"))
                    self._active.pop(key, None)
                    self._cooldown_until[key] = reading.received_at + timedelta(
                        seconds=rule.cooldown_seconds
                    )

        for metric, value in current_values.items():
            self._previous_values[(reading.location, metric)] = value
        return points

    def _gas_event_is_ready(self, reading: AirQualityReading, metric: str) -> bool:
        uptime = reading.sensor_uptime_s
        if uptime is None:
            return True
        if metric == "voc_index":
            return uptime >= VOC_RELIABLE_EVENT_SECONDS
        if metric == "nox_index":
            return uptime >= NOX_RELIABLE_EVENT_SECONDS
        return True

    def _process_invalid(self, reading: AirQualityReading) -> list[PointData]:
        key = (reading.location, "sensor_invalid")
        active = self._active.get(key)
        if active is not None:
            if reading.received_at <= (active.last_observed or active.start):
                return []
            active.sample_count += 1
            active.last_observed = reading.received_at
            return []
        cooldown_until = self._cooldown_until.get(key)
        if cooldown_until is not None and reading.received_at < cooldown_until:
            return []
        rule = SENSOR_INVALID_EVENT_RULE
        active = ActiveEvent(
            rule,
            reading.location,
            reading.topic,
            reading.node_id,
            reading.received_at,
            1.0,
            1.0,
            None,
            None,
            last_observed=reading.received_at,
        )
        self._active[key] = active
        return [_event_point(active, reading.received_at, "active")]

    def _clear_invalid(self, reading: AirQualityReading) -> list[PointData]:
        key = (reading.location, "sensor_invalid")
        active = self._active.get(key)
        if active is None:
            return []
        if reading.received_at <= (active.last_observed or active.start):
            return []
        self._active.pop(key, None)
        self._cooldown_until[key] = reading.received_at + timedelta(
            seconds=SENSOR_INVALID_EVENT_RULE.cooldown_seconds
        )
        return [_event_point(active, reading.received_at, "completed")]

    def snapshots(self, now: datetime) -> tuple[PointData, ...]:
        """Return same-identity active points for graceful shutdown state."""

        return tuple(
            _event_point(active, now, "active")
            for active in self._active.values()
        )


class AirQualityPipeline:
    """Maintain one aligned aggregation window per SEN66 location."""

    def __init__(self, *, expected_publish_seconds: int = 5) -> None:
        if expected_publish_seconds <= 0 or WINDOW_SECONDS % expected_publish_seconds:
            raise ValueError("expected publish interval must evenly divide 15 minutes")
        self.expected_publish_seconds = expected_publish_seconds
        self._windows: dict[str, WindowAccumulator] = {}
        self._previous_window_means: dict[str, dict[str, float]] = {}
        self._seen_order: dict[str, deque[tuple[int, int]]] = {}
        self._seen: dict[str, set[tuple[int, int]]] = {}
        self._events = EventDetector()

    def process(
        self,
        reading: AirQualityReading,
        *,
        detect_events: bool = True,
    ) -> PipelineResult:
        if self._is_duplicate(reading):
            return PipelineResult(False, duplicate=True)

        start = aligned_window_start(reading.received_at)
        active = self._windows.get(reading.location)
        aggregates: list[PointData] = []
        if active is not None and start < active.window_start:
            return PipelineResult(False, late=True)
        if active is None or start > active.window_start:
            if active is not None:
                aggregates.append(
                    active.point(
                        expected_publish_seconds=self.expected_publish_seconds,
                        partial=False,
                    )
                )
                self._previous_window_means[reading.location] = active.means()
            active = WindowAccumulator(
                reading.location,
                reading.topic,
                reading.node_id,
                start,
                start + timedelta(seconds=WINDOW_SECONDS),
            )
            self._windows[reading.location] = active

        active.add(reading)
        events = (
            self._events.process(
                reading, self._previous_window_means.get(reading.location, {})
            )
            if detect_events
            else []
        )
        return PipelineResult(
            True,
            aggregate_points=tuple(aggregates),
            event_points=tuple(events),
        )

    def flush_partial(self, now: datetime | None = None) -> tuple[PointData, ...]:
        now = now or _utc_now()
        return tuple(
            window.point(
                expected_publish_seconds=self.expected_publish_seconds,
                partial=now < window.window_end,
                observed_until=now,
            )
            for window in self._windows.values()
            if window.sample_count
        )

    def restore_active_events(self, rows: Iterable[Mapping[str, Any]]) -> int:
        """Restore event detector state from long-term event records."""

        return self._events.restore(rows)

    def flush_active_events(self, now: datetime | None = None) -> tuple[PointData, ...]:
        """Persist active detector state without closing the episodes."""

        return self._events.snapshots(now or _utc_now())

    def _is_duplicate(self, reading: AirQualityReading) -> bool:
        # A sequence alone is not a stable packet identity because legacy
        # schema-v1 firmware resets it at boot. Schema v2 supplies both parts.
        if reading.sequence is None or reading.boot_id is None:
            return False
        packet = (reading.boot_id, reading.sequence)
        seen = self._seen.setdefault(reading.location, set())
        if packet in seen:
            return True
        order = self._seen_order.setdefault(reading.location, deque())
        seen.add(packet)
        order.append(packet)
        while len(order) > 4096:
            seen.discard(order.popleft())
        return False


def aligned_window_start(value: datetime) -> datetime:
    """Return the UTC-aligned 00/15/30/45 minute boundary."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    minute = (value.minute // 15) * 15
    return value.replace(minute=minute, second=0, microsecond=0)


def _event_point(active: ActiveEvent, end: datetime, state: str) -> PointData:
    rule = active.rule
    duration = max(0.0, (end - active.start).total_seconds())
    fields: dict[str, float | int | bool | str] = {
        "event_id": f"{active.location}:{rule.event_type}:{int(active.start.timestamp())}",
        "threshold": rule.trigger,
        "clear_threshold": rule.clear,
        "trigger_value": active.trigger_value,
        "peak_value": active.peak_value,
        "event_start": _iso(active.start),
        "event_end": _iso(end) if state == "completed" else "",
        "duration_seconds": round(duration, 3),
        "sample_count": active.sample_count,
        "last_observed": _iso(active.last_observed or active.start),
        "severity": rule.severity,
        "framework": rule.framework,
        "status_type": rule.status_type,
        "evaluated_window": rule.evaluated_window,
        "source_name": rule.source_name,
        "source_document": rule.source_document,
        "state": state,
    }
    if active.baseline_value is not None:
        fields["baseline_value"] = active.baseline_value
    if active.preceding_window_value is not None:
        fields["preceding_window_value"] = active.preceding_window_value
        fields["difference_from_preceding_window"] = (
            active.trigger_value - active.preceding_window_value
        )
    if rule.metric == "voc_index":
        fields["difference_from_100"] = active.trigger_value - 100.0

    tags = {
        "location": active.location,
        "topic": active.topic,
        "sensor_type": "air_quality",
        "event_type": rule.event_type,
        "metric": rule.metric,
    }
    if active.node_id is not None:
        tags["node_id"] = str(active.node_id)
    return PointData("air_quality_event", tags, fields, active.start)


def _add_stats(
    fields: dict[str, float | int | bool | str],
    metric: str,
    values: Iterable[float],
    *,
    mean: bool = False,
    minimum: bool = False,
    maximum: bool = False,
) -> None:
    sequence = list(values)
    if not sequence:
        return
    if mean:
        fields[f"{metric}_mean"] = round(sum(sequence) / len(sequence), 4)
    if minimum:
        fields[f"{metric}_min"] = round(min(sequence), 4)
    if maximum:
        fields[f"{metric}_max"] = round(max(sequence), 4)


def _percentile(values: Iterable[float], percentile: float) -> float:
    sequence = sorted(values)
    if not sequence:
        raise ValueError("percentile requires at least one value")
    if len(sequence) == 1:
        return sequence[0]
    position = (len(sequence) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sequence[lower]
    fraction = position - lower
    return sequence[lower] + (sequence[upper] - sequence[lower]) * fraction


def _finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _number(value: Any) -> float | None:
    return float(value) if _finite(value) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif value:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
