from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.models import AirQualityReading
from bridge.air_quality_pipeline import PointData, aggregate_completed_windows
from migrations.backfill_air_quality_15m import (
    BackfillOptions,
    ExistingAggregate,
    aggregate_matches,
    run_backfill,
)


BASE = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def reading(
    seconds: int,
    *,
    sequence: int | None = None,
    boot_id: int | None = 1,
    valid: bool = True,
) -> AirQualityReading:
    return AirQualityReading(
        topic="home/air/office",
        location="office",
        co2=700,
        pm1=2.0,
        pm25=5.0 if valid else None,
        pm4=4.0,
        pm10=10.0,
        voc_index=100,
        nox_index=1,
        temperature_c=23.0,
        humidity=45.0,
        received_at=BASE + timedelta(seconds=seconds),
        node_id=100,
        sequence=seconds if sequence is None else sequence,
        boot_id=boot_id,
        sensor_uptime_s=30_000 + seconds,
        sraw_voc=25_000,
        sraw_nox=20_000,
    )


def generated_point(samples: list[AirQualityReading]) -> PointData:
    return aggregate_completed_windows(
        samples,
        completed_before=BASE + timedelta(minutes=15),
    ).aggregate_points[0]


def existing_from_point(
    point: PointData, *, fields: dict[str, object] | None = None
) -> ExistingAggregate:
    return ExistingAggregate(
        location=str(point.tags["location"]),
        timestamp=point.timestamp,
        tags=dict(point.tags),
        fields=dict(point.fields) if fields is None else fields,
    )


class FakeStore:
    def __init__(
        self,
        raw: list[AirQualityReading],
        existing: list[ExistingAggregate] | None = None,
    ) -> None:
        self.raw = raw
        self.existing = list(existing or [])
        self.writes: list[PointData] = []
        self.events_touched = False

    def raw_bounds(self, _location: str | None):
        if not self.raw:
            return None
        return min(item.received_at for item in self.raw), max(
            item.received_at for item in self.raw
        )

    def read_raw(self, start, stop, location):
        return [
            item
            for item in self.raw
            if start <= item.received_at < stop
            and (location is None or item.location == location)
        ]

    def read_aggregates(self, start, stop, location):
        return [
            item
            for item in self.existing
            if start <= item.timestamp < stop
            and (location is None or item.location == location)
        ]

    def write_aggregates(self, points):
        for point in points:
            self.writes.append(point)
            key = (str(point.tags["location"]), point.timestamp)
            self.existing = [
                item
                for item in self.existing
                if (item.location, item.timestamp) != key
            ]
            self.existing.append(existing_from_point(point))

    def close(self):
        return None


def options(*, write: bool = False, repair: bool = False) -> BackfillOptions:
    return BackfillOptions(
        start=BASE,
        end=BASE + timedelta(minutes=15),
        batch_duration=timedelta(minutes=15),
        write=write,
        repair=repair,
    )


class AirQualityBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.samples = [reading(0), reading(5), reading(10, valid=False)]
        self.point = generated_point(self.samples)

    def test_dry_run_performs_no_writes_and_reports_missing(self) -> None:
        store = FakeStore(self.samples)

        summary = run_backfill(
            store, options(), expected_publish_seconds=5, emit=lambda _line: None
        )

        self.assertEqual(summary.windows_missing, 1)
        self.assertEqual(summary.windows_written, 0)
        self.assertEqual(store.writes, [])
        self.assertFalse(store.events_touched)

    def test_existing_correct_window_is_skipped(self) -> None:
        store = FakeStore(self.samples, [existing_from_point(self.point)])

        summary = run_backfill(
            store,
            options(write=True),
            expected_publish_seconds=5,
            emit=lambda _line: None,
        )

        self.assertEqual(summary.windows_already_correct, 1)
        self.assertEqual(summary.windows_written, 0)
        self.assertEqual(store.writes, [])

    def test_incomplete_window_is_repaired_only_when_explicit(self) -> None:
        fields = dict(self.point.fields)
        fields["is_partial"] = True
        fields["expected_sample_count"] = 3
        store = FakeStore(self.samples, [existing_from_point(self.point, fields=fields)])

        summary = run_backfill(
            store,
            options(write=True, repair=True),
            expected_publish_seconds=5,
            emit=lambda _line: None,
        )

        self.assertEqual(summary.windows_incomplete, 1)
        self.assertEqual(summary.windows_repaired, 1)
        self.assertEqual(summary.windows_written, 1)
        self.assertTrue(aggregate_matches(self.point, store.existing[0]))

    def test_write_is_idempotent_on_second_execution(self) -> None:
        store = FakeStore(self.samples)
        first = run_backfill(
            store,
            options(write=True),
            expected_publish_seconds=5,
            emit=lambda _line: None,
        )
        writes_after_first = len(store.writes)
        second = run_backfill(
            store,
            options(write=True),
            expected_publish_seconds=5,
            emit=lambda _line: None,
        )

        self.assertEqual(first.windows_written, 1)
        self.assertEqual(writes_after_first, 1)
        self.assertEqual(second.windows_already_correct, 1)
        self.assertEqual(len(store.writes), 1)

    def test_duplicate_source_copy_is_not_double_counted(self) -> None:
        store = FakeStore([self.samples[0], self.samples[0]])

        summary = run_backfill(
            store, options(), expected_publish_seconds=5, emit=lambda _line: None
        )

        self.assertEqual(summary.duplicate_samples, 1)
        self.assertEqual(summary.valid_samples, 1)

    def test_samples_in_current_incomplete_window_are_excluded(self) -> None:
        store = FakeStore([reading(905)])

        summary = run_backfill(
            store, options(), expected_publish_seconds=5, emit=lambda _line: None
        )

        self.assertEqual(summary.windows_discovered, 0)
        self.assertEqual(summary.windows_written, 0)


if __name__ == "__main__":
    unittest.main()
