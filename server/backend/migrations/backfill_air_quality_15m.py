"""Backfill and verify permanent SEN66 15-minute aggregates.

Run from ``server/backend`` with ``python -m migrations.backfill_air_quality_15m``.
The default mode is a read-only dry run. Historical event records are never read,
rewritten, inferred, or deleted by this utility.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

from app.config import AppSettings, load_settings
from app.influx import (
    AIR_QUALITY_STORAGE_FIELDS,
    air_quality_reading_from_values,
)
from app.models import AirQualityReading
from bridge.air_quality_pipeline import (
    ALL_METRICS,
    MEAN_MAX_METRICS,
    MEAN_MIN_MAX_METRICS,
    MEAN_ONLY_METRICS,
    P95_METRICS,
    WINDOW_SECONDS,
    PointData,
    aggregate_completed_windows,
    aligned_window_start,
)


RAW_MEASUREMENT = "air_quality_reading"
AGGREGATE_MEASUREMENT = "air_quality_15m"
LOCATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IDENTITY_FIELDS = (
    "window_start",
    "window_end",
    "sample_count",
    "valid_sample_count",
    "invalid_sample_count",
    "expected_sample_count",
    "data_coverage",
    "is_partial",
)
AGGREGATE_FIELDS = frozenset(
    IDENTITY_FIELDS
    + tuple(f"{metric}_mean" for metric in ALL_METRICS)
    + tuple(f"{metric}_min" for metric in MEAN_MIN_MAX_METRICS)
    + tuple(f"{metric}_max" for metric in MEAN_MAX_METRICS + MEAN_MIN_MAX_METRICS)
    + tuple(f"{metric}_p95" for metric in P95_METRICS)
)


@dataclass(frozen=True)
class ExistingAggregate:
    location: str
    timestamp: datetime
    tags: Mapping[str, str]
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class BackfillOptions:
    start: datetime
    end: datetime
    batch_duration: timedelta
    location: str | None = None
    write: bool = False
    verify_only: bool = False
    repair: bool = False
    verbose: bool = False


@dataclass
class BackfillSummary:
    raw_samples_read: int = 0
    valid_samples: int = 0
    invalid_samples: int = 0
    duplicate_samples: int = 0
    late_samples: int = 0
    windows_discovered: int = 0
    windows_already_correct: int = 0
    windows_missing: int = 0
    windows_incomplete: int = 0
    windows_malformed: int = 0
    windows_repaired: int = 0
    windows_written: int = 0
    windows_skipped: int = 0
    unrecoverable_windows: int = 0
    windows_without_raw: int = 0
    historical_gaps: int = 0

    @property
    def unresolved_windows(self) -> int:
        missing_writes = self.windows_written - self.windows_repaired
        return max(0, self.windows_missing - missing_writes) + max(
            0,
            self.windows_incomplete
            + self.windows_malformed
            - self.windows_repaired,
        ) + self.unrecoverable_windows

    def render(self) -> str:
        values = (
            ("raw samples read", self.raw_samples_read),
            ("valid samples", self.valid_samples),
            ("invalid samples", self.invalid_samples),
            ("duplicate samples", self.duplicate_samples),
            ("late samples", self.late_samples),
            ("windows discovered", self.windows_discovered),
            ("windows already correct", self.windows_already_correct),
            ("windows missing", self.windows_missing),
            ("windows incomplete", self.windows_incomplete),
            ("windows malformed", self.windows_malformed),
            ("windows repaired", self.windows_repaired),
            ("windows written", self.windows_written),
            ("windows skipped", self.windows_skipped),
            ("unrecoverable windows", self.unrecoverable_windows),
            ("aggregate windows without retained raw", self.windows_without_raw),
            ("historical gaps", self.historical_gaps),
            ("unresolved windows", self.unresolved_windows),
        )
        return "\n".join(f"{label}: {value}" for label, value in values)


class BackfillStore(Protocol):
    def raw_bounds(self, location: str | None) -> tuple[datetime, datetime] | None:
        ...

    def read_raw(
        self, start: datetime, stop: datetime, location: str | None
    ) -> list[AirQualityReading]:
        ...

    def read_aggregates(
        self, start: datetime, stop: datetime, location: str | None
    ) -> list[ExistingAggregate]:
        ...

    def write_aggregates(self, points: Iterable[PointData]) -> None:
        ...

    def close(self) -> None:
        ...


class InfluxBackfillStore:
    """Bounded InfluxDB reader/writer for the maintained migration."""

    def __init__(self, settings: AppSettings) -> None:
        influx = settings.influx
        self._org = influx.org
        self._permanent_bucket = influx.bucket
        self._source_buckets = tuple(dict.fromkeys((influx.bucket, influx.live_bucket)))
        self._read_client = InfluxDBClient(
            url=influx.url,
            token=influx.read_token or influx.write_token,
            org=influx.org,
            timeout=120_000,
        )
        self._write_client = InfluxDBClient(
            url=influx.url,
            token=influx.write_token,
            org=influx.org,
            timeout=120_000,
        )
        self._query_api = self._read_client.query_api()
        self._write_api = self._write_client.write_api(write_options=SYNCHRONOUS)

    def raw_bounds(self, location: str | None) -> tuple[datetime, datetime] | None:
        first: list[datetime] = []
        last: list[datetime] = []
        location_filter = _location_filter(location)
        for bucket in self._source_buckets:
            base = f'''from(bucket: {_flux_string(bucket)})
  |> range(start: 0)
  |> filter(fn: (r) => r._measurement == {_flux_string(RAW_MEASUREMENT)} and r._field == "co2")
{location_filter}  |> group()
'''
            first.extend(record.get_time() for record in self._query(base + "  |> first()\n"))
            last.extend(record.get_time() for record in self._query(base + "  |> last()\n"))
        if not first or not last:
            return None
        return min(first), max(last)

    def read_raw(
        self, start: datetime, stop: datetime, location: str | None
    ) -> list[AirQualityReading]:
        readings: list[AirQualityReading] = []
        location_filter = _location_filter(location)
        fields = json.dumps(list(AIR_QUALITY_STORAGE_FIELDS))
        for bucket in self._source_buckets:
            flux = f'''from(bucket: {_flux_string(bucket)})
  |> range(start: {_flux_time(start)}, stop: {_flux_time(stop)})
  |> filter(fn: (r) => r._measurement == {_flux_string(RAW_MEASUREMENT)})
  |> filter(fn: (r) => contains(value: r._field, set: {fields}))
{location_filter}  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
            for record in self._query(flux):
                reading = air_quality_reading_from_values(record.values)
                if reading is not None:
                    readings.append(reading)
        return sorted(readings, key=lambda reading: reading.received_at)

    def read_aggregates(
        self, start: datetime, stop: datetime, location: str | None
    ) -> list[ExistingAggregate]:
        flux = f'''from(bucket: {_flux_string(self._permanent_bucket)})
  |> range(start: {_flux_time(start)}, stop: {_flux_time(stop)})
  |> filter(fn: (r) => r._measurement == {_flux_string(AGGREGATE_MEASUREMENT)})
{_location_filter(location)}  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
        result: list[ExistingAggregate] = []
        for record in self._query(flux):
            values = record.values
            timestamp = values.get("_time")
            location_value = values.get("location")
            if not isinstance(timestamp, datetime) or not location_value:
                continue
            tags = {
                key: str(values[key])
                for key in ("location", "topic", "sensor_type", "node_id")
                if values.get(key) not in (None, "")
            }
            fields = {key: values[key] for key in AGGREGATE_FIELDS if key in values}
            result.append(
                ExistingAggregate(
                    location=str(location_value),
                    timestamp=_utc(timestamp),
                    tags=tags,
                    fields=fields,
                )
            )
        return result

    def write_aggregates(self, points: Iterable[PointData]) -> None:
        influx_points = []
        for item in points:
            point = Point(item.measurement)
            for key, value in item.tags.items():
                point = point.tag(key, value)
            for key, value in item.fields.items():
                point = point.field(key, value)
            influx_points.append(point.time(item.timestamp))
        if influx_points:
            self._write_api.write(
                bucket=self._permanent_bucket,
                org=self._org,
                record=influx_points,
            )

    def close(self) -> None:
        self._read_client.close()
        self._write_client.close()

    def _query(self, flux: str) -> list[Any]:
        return [
            record
            for table in self._query_api.query(query=flux, org=self._org)
            for record in table.records
        ]


def run_backfill(
    store: BackfillStore,
    options: BackfillOptions,
    *,
    expected_publish_seconds: int,
    emit: Callable[[str], None] = print,
) -> BackfillSummary:
    """Process bounded aligned batches and reconcile each logical window."""

    _validate_options(options)
    summary = BackfillSummary()
    previous_window: dict[str, datetime] = {}
    cursor = options.start
    batch_number = 0
    while cursor < options.end:
        batch_number += 1
        stop = min(options.end, cursor + options.batch_duration)
        raw_with_duplicates = store.read_raw(cursor, stop, options.location)
        raw, source_duplicates = deduplicate_readings(raw_with_duplicates)
        generated = aggregate_completed_windows(
            raw,
            completed_before=stop,
            expected_publish_seconds=expected_publish_seconds,
        )
        existing = store.read_aggregates(cursor, stop, options.location)

        summary.raw_samples_read += len(raw_with_duplicates)
        summary.valid_samples += generated.valid_sample_count
        summary.invalid_samples += generated.invalid_sample_count
        summary.duplicate_samples += source_duplicates + generated.duplicate_sample_count
        summary.late_samples += generated.late_sample_count
        summary.windows_discovered += len(generated.aggregate_points)

        generated_keys = {
            (str(point.tags["location"]), _utc(point.timestamp))
            for point in generated.aggregate_points
        }
        existing_by_key: dict[tuple[str, datetime], list[ExistingAggregate]] = {}
        for row in existing:
            existing_by_key.setdefault((row.location, _utc(row.timestamp)), []).append(row)
        summary.windows_without_raw += len(set(existing_by_key) - generated_keys)

        planned: list[PointData] = []
        for point in generated.aggregate_points:
            location = str(point.tags["location"])
            timestamp = _utc(point.timestamp)
            prior = previous_window.get(location)
            if prior is not None and timestamp > prior + timedelta(seconds=WINDOW_SECONDS):
                missing = int(
                    (timestamp - prior).total_seconds() // WINDOW_SECONDS
                ) - 1
                summary.historical_gaps += missing
                emit(
                    f"gap location={location} start={_iso(prior + timedelta(seconds=WINDOW_SECONDS))} "
                    f"stop={_iso(timestamp)} missing_windows={missing}"
                )
            previous_window[location] = timestamp

            rows = existing_by_key.get((location, timestamp), [])
            status = "missing"
            if not rows:
                summary.windows_missing += 1
                if options.write:
                    planned.append(point)
                else:
                    summary.windows_skipped += 1
            elif len(rows) > 1:
                status = "duplicate_existing"
                summary.unrecoverable_windows += 1
                summary.windows_skipped += 1
            elif aggregate_matches(point, rows[0]):
                status = "correct"
                summary.windows_already_correct += 1
                summary.windows_skipped += 1
            else:
                incomplete = rows[0].fields.get("is_partial") is True
                status = "incomplete" if incomplete else "malformed"
                if incomplete:
                    summary.windows_incomplete += 1
                else:
                    summary.windows_malformed += 1
                if options.write and options.repair:
                    planned.append(
                        PointData(
                            measurement=point.measurement,
                            tags=rows[0].tags,
                            fields=point.fields,
                            timestamp=point.timestamp,
                        )
                    )
                    summary.windows_repaired += 1
                else:
                    summary.windows_skipped += 1
            if options.verbose or status != "correct":
                emit(
                    f"window location={location} start={_iso(timestamp)} "
                    f"samples={point.fields['sample_count']} valid={point.fields['valid_sample_count']} "
                    f"invalid={point.fields['invalid_sample_count']} status={status}"
                )

        if planned:
            store.write_aggregates(planned)
            summary.windows_written += len(planned)
        emit(
            f"batch={batch_number} start={_iso(cursor)} stop={_iso(stop)} "
            f"raw={len(raw_with_duplicates)} windows={len(generated.aggregate_points)} "
            f"existing={len(existing_by_key)} writes={len(planned)}"
        )
        cursor = stop

    return summary


def deduplicate_readings(
    readings: Sequence[AirQualityReading],
) -> tuple[list[AirQualityReading], int]:
    """Remove stable packet identities and cross-bucket same-point copies."""

    stable_seen: set[tuple[str, int, int]] = set()
    point_seen: set[tuple[str, datetime]] = set()
    result: list[AirQualityReading] = []
    duplicates = 0
    for reading in sorted(readings, key=lambda item: item.received_at):
        point_key = (reading.location, _utc(reading.received_at))
        stable_key = None
        if reading.boot_id is not None and reading.sequence is not None:
            stable_key = (reading.location, reading.boot_id, reading.sequence)
        if point_key in point_seen or (
            stable_key is not None and stable_key in stable_seen
        ):
            duplicates += 1
            continue
        point_seen.add(point_key)
        if stable_key is not None:
            stable_seen.add(stable_key)
        result.append(reading)
    return result, duplicates


def aggregate_matches(expected: PointData, existing: ExistingAggregate) -> bool:
    """Compare all fields emitted by the shared live aggregation code."""

    if existing.location != str(expected.tags.get("location", "")):
        return False
    if _utc(existing.timestamp) != _utc(expected.timestamp):
        return False
    for key, expected_value in expected.fields.items():
        if key not in existing.fields:
            return False
        actual_value = existing.fields[key]
        if isinstance(expected_value, bool):
            if actual_value is not expected_value:
                return False
        elif isinstance(expected_value, (int, float)) and not isinstance(
            expected_value, bool
        ):
            if isinstance(actual_value, bool) or not isinstance(actual_value, (int, float)):
                return False
            if not math.isclose(
                float(actual_value), float(expected_value), rel_tol=1e-9, abs_tol=1e-4
            ):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _validate_options(options: BackfillOptions) -> None:
    for name, value in (("start", options.start), ("end", options.end)):
        if _utc(value) != aligned_window_start(value):
            raise ValueError(f"{name} must be aligned to a UTC 15-minute boundary")
    if options.start >= options.end:
        raise ValueError("start must be earlier than end")
    seconds = options.batch_duration.total_seconds()
    if seconds < WINDOW_SECONDS or seconds % WINDOW_SECONDS:
        raise ValueError("batch duration must be a positive multiple of 15 minutes")
    if options.location is not None and not LOCATION_RE.fullmatch(options.location):
        raise ValueError("location must be a stable slug")
    if options.repair and not options.write:
        raise ValueError("--repair requires --write")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {value}") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _flux_time(value: datetime) -> str:
    return f"time(v: {_flux_string(_iso(value))})"


def _location_filter(location: str | None) -> str:
    if location is None:
        return ""
    return f"  |> filter(fn: (r) => r.location == {_flux_string(location)})\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=_parse_time, help="aligned inclusive UTC start")
    parser.add_argument("--end", type=_parse_time, help="aligned exclusive UTC end")
    parser.add_argument("--location", help="optional SEN66 location slug")
    parser.add_argument(
        "--batch-hours",
        type=float,
        default=24.0,
        help="bounded batch size; must be a multiple of 0.25 (default: 24)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read and report only (default)")
    mode.add_argument("--verify-only", action="store_true", help="read only; fail if repairable windows remain")
    mode.add_argument("--write", action="store_true", help="write missing aggregate windows")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="with --write, overwrite one incomplete/malformed logical window",
    )
    parser.add_argument("--verbose", action="store_true", help="print every discovered window")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="configuration file (default: backend/.env)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings(args.env_file) if args.env_file else load_settings()
    store: BackfillStore = InfluxBackfillStore(settings)
    try:
        bounds = store.raw_bounds(args.location)
        if bounds is None:
            print("No raw SEN66 samples were found in either configured bucket.")
            return 0
        current_boundary = aligned_window_start(datetime.now(timezone.utc))
        start = args.start or aligned_window_start(bounds[0])
        end = args.end or current_boundary
        if end > current_boundary:
            raise ValueError(
                f"end {_iso(end)} includes the current incomplete window; "
                f"use an end at or before {_iso(current_boundary)}"
            )
        options = BackfillOptions(
            start=start,
            end=end,
            batch_duration=timedelta(hours=args.batch_hours),
            location=args.location,
            write=args.write,
            verify_only=args.verify_only,
            repair=args.repair,
            verbose=args.verbose,
        )
        mode = "write" if args.write else "verify-only" if args.verify_only else "dry-run"
        print(
            f"mode={mode} start={_iso(start)} end={_iso(end)} "
            f"batch_hours={args.batch_hours:g} location={args.location or 'all'}"
        )
        print(
            f"raw_bounds earliest={_iso(bounds[0])} latest={_iso(bounds[1])} "
            f"source_buckets={settings.influx.bucket},{settings.influx.live_bucket} "
            f"destination={settings.influx.bucket}/{AGGREGATE_MEASUREMENT}"
        )
        summary = run_backfill(
            store,
            options,
            expected_publish_seconds=settings.air_quality.expected_publish_seconds,
        )
        print("SUMMARY")
        print(summary.render())
        if summary.late_samples:
            return 2
        if args.verify_only and summary.unresolved_windows:
            return 1
        if args.write and summary.unresolved_windows:
            return 1
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
