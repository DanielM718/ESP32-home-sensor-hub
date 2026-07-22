from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.models import AirQualityReading
from bridge.air_quality_pipeline import (
    AirQualityPipeline,
    aggregate_completed_windows,
    aligned_window_start,
)


BASE = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)


def reading(
    seconds: int,
    *,
    sequence: int | None = None,
    boot_id: int | None = 7,
    voc_index: int | None = 100,
    nox_index: int | None = 1,
    pm25: float | None = 5.0,
    pm10: float | None = 10.0,
    co2: int | None = 700,
    status_flags: int | None = None,
) -> AirQualityReading:
    return AirQualityReading(
        topic="home/air/office",
        location="office",
        co2=co2,
        pm1=2.0,
        pm25=pm25,
        pm4=4.0,
        pm10=pm10,
        voc_index=voc_index,
        nox_index=nox_index,
        temperature_c=23.0,
        humidity=45.0,
        received_at=BASE + timedelta(seconds=seconds),
        node_id=100,
        sequence=seconds if sequence is None else sequence,
        boot_id=boot_id,
        sensor_uptime_s=30_000 + seconds,
        status_flags=status_flags,
        sraw_voc=25_000,
        sraw_nox=20_000,
    )


class AirQualityPipelineTest(unittest.TestCase):
    def test_utc_15_minute_alignment(self) -> None:
        value = datetime(2026, 7, 21, 12, 29, 59, tzinfo=timezone.utc)
        self.assertEqual(
            aligned_window_start(value),
            datetime(2026, 7, 21, 12, 15, tzinfo=timezone.utc),
        )

    def test_completed_window_has_means_min_max_p95_and_coverage(self) -> None:
        pipeline = AirQualityPipeline(expected_publish_seconds=5)
        pipeline.process(reading(0, voc_index=90, pm25=0))
        pipeline.process(reading(5, voc_index=100, pm25=100))
        result = pipeline.process(reading(900, voc_index=110, pm25=5))
        fields = result.aggregate_points[0].fields

        self.assertEqual(fields["voc_index_mean"], 95.0)
        self.assertEqual(fields["voc_index_min"], 90.0)
        self.assertEqual(fields["voc_index_max"], 100.0)
        self.assertEqual(fields["pm25_mean"], 50.0)
        self.assertEqual(fields["pm25_max"], 100.0)
        self.assertEqual(fields["pm25_p95"], 95.0)
        self.assertEqual(fields["expected_sample_count"], 180)
        self.assertEqual(fields["valid_sample_count"], 2)
        self.assertEqual(fields["data_coverage"], 1.11)

    def test_short_spikes_survive_in_window_maximum(self) -> None:
        pipeline = AirQualityPipeline()
        for second in range(0, 900, 5):
            pipeline.process(
                reading(
                    second,
                    voc_index=300 if 300 <= second < 360 else 100,
                    pm25=80 if 300 <= second < 360 else 5,
                )
            )
        result = pipeline.process(reading(900))
        fields = result.aggregate_points[0].fields

        self.assertLess(fields["voc_index_mean"], 120)
        self.assertEqual(fields["voc_index_max"], 300)
        self.assertEqual(fields["pm25_max"], 80)

    def test_missing_values_are_invalid_and_never_zero_filled(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(0, pm25=None, co2=900))
        pipeline.process(reading(5, pm25=10))
        fields = pipeline.process(reading(900)).aggregate_points[0].fields

        self.assertEqual(fields["sample_count"], 2)
        self.assertEqual(fields["valid_sample_count"], 1)
        self.assertEqual(fields["invalid_sample_count"], 1)
        self.assertEqual(fields["pm25_mean"], 10)
        self.assertEqual(fields["co2_mean"], 700)

    def test_duplicate_packet_is_not_counted_twice(self) -> None:
        pipeline = AirQualityPipeline()
        sample = reading(0, sequence=42)
        self.assertTrue(pipeline.process(sample).accepted)
        duplicate = pipeline.process(sample)
        self.assertTrue(duplicate.duplicate)
        fields = pipeline.flush_partial(BASE + timedelta(seconds=5))[0].fields
        self.assertEqual(fields["sample_count"], 1)

    def test_legacy_sequence_without_boot_id_is_not_mistaken_for_duplicate(self) -> None:
        pipeline = AirQualityPipeline()

        self.assertTrue(pipeline.process(reading(0, sequence=1, boot_id=None)).accepted)
        self.assertTrue(pipeline.process(reading(5, sequence=1, boot_id=None)).accepted)
        fields = pipeline.flush_partial(BASE + timedelta(seconds=5))[0].fields
        self.assertEqual(fields["sample_count"], 2)

    def test_out_of_order_within_window_is_accepted_but_closed_window_is_late(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(20, sequence=20))
        within = pipeline.process(reading(10, sequence=10))
        self.assertTrue(within.accepted)
        pipeline.process(reading(900, sequence=900))
        late = pipeline.process(reading(30, sequence=30))
        self.assertTrue(late.late)

    def test_partial_window_is_persistable_and_restart_can_reconstruct(self) -> None:
        samples = [reading(0), reading(5), reading(10)]
        first = AirQualityPipeline()
        for sample in samples:
            first.process(sample)
        partial = first.flush_partial(BASE + timedelta(seconds=10))[0]
        self.assertTrue(partial.fields["is_partial"])
        self.assertEqual(partial.fields["sample_count"], 3)
        self.assertEqual(partial.fields["expected_sample_count"], 3)
        self.assertEqual(partial.fields["data_coverage"], 100.0)

        restarted = AirQualityPipeline()
        for sample in samples:
            restarted.process(sample)
        completed = restarted.process(reading(900)).aggregate_points[0]
        self.assertFalse(completed.fields["is_partial"])
        self.assertEqual(completed.fields["sample_count"], 3)

    def test_shutdown_after_window_end_persists_a_completed_low_coverage_window(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(0))

        point = pipeline.flush_partial(BASE + timedelta(seconds=901))[0]

        self.assertFalse(point.fields["is_partial"])
        self.assertEqual(point.fields["expected_sample_count"], 180)
        self.assertEqual(point.fields["valid_sample_count"], 1)

    def test_shared_backfill_aggregation_matches_live_output(self) -> None:
        samples = [reading(0), reading(5, voc_index=120), reading(10, pm25=None)]
        live = AirQualityPipeline()
        for sample in samples:
            live.process(sample)
        live_point = live.process(reading(900)).aggregate_points[0]

        backfill = aggregate_completed_windows(
            samples,
            completed_before=BASE + timedelta(minutes=15),
        )

        self.assertEqual(backfill.aggregate_points, (live_point,))
        self.assertEqual(backfill.valid_sample_count, 2)
        self.assertEqual(backfill.invalid_sample_count, 1)

    def test_shared_backfill_excludes_incomplete_window(self) -> None:
        result = aggregate_completed_windows(
            [reading(905)],
            completed_before=BASE + timedelta(minutes=15),
        )

        self.assertFalse(result.aggregate_points)

    def test_shared_backfill_recovery_does_not_double_count_packet(self) -> None:
        sample = reading(0, sequence=42, boot_id=99)
        result = aggregate_completed_windows(
            [sample, sample],
            completed_before=BASE + timedelta(minutes=15),
        )

        self.assertEqual(result.aggregate_points[0].fields["sample_count"], 1)
        self.assertEqual(result.duplicate_sample_count, 1)

    def test_threshold_event_has_single_trigger_peak_hysteresis_and_cooldown(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(0, voc_index=100))
        triggered = pipeline.process(reading(5, voc_index=160))
        trigger_points = [
            point for point in triggered.event_points
            if point.tags.get("event_type") == "voc_action_level"
        ]
        self.assertEqual(len(trigger_points), 1)
        self.assertEqual(trigger_points[0].fields["state"], "active")

        sustained = pipeline.process(reading(10, voc_index=180))
        self.assertFalse(any(
            point.tags.get("event_type") == "voc_action_level"
            for point in sustained.event_points
        ))
        completed = pipeline.process(reading(15, voc_index=120))
        event = next(
            point for point in completed.event_points
            if point.tags.get("event_type") == "voc_action_level"
        )
        self.assertEqual(event.fields["state"], "completed")
        self.assertEqual(event.fields["peak_value"], 180)
        self.assertEqual(event.fields["duration_seconds"], 10)
        self.assertEqual(event.fields["difference_from_100"], 60)

        cooldown = pipeline.process(reading(20, voc_index=160))
        self.assertFalse(any(
            point.tags.get("event_type") == "voc_action_level"
            for point in cooldown.event_points
        ))

    def test_voc_event_includes_preceding_window_context(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(0, voc_index=90))
        result = pipeline.process(reading(900, voc_index=160))
        event = next(
            point for point in result.event_points
            if point.tags.get("event_type") == "voc_action_level"
        )
        self.assertEqual(event.fields["preceding_window_value"], 90)
        self.assertEqual(event.fields["difference_from_preceding_window"], 70)

    def test_active_event_restores_across_a_long_bridge_restart(self) -> None:
        original = AirQualityPipeline()
        triggered = original.process(reading(0, voc_index=160))
        point = next(
            item
            for item in triggered.event_points
            if item.tags.get("event_type") == "voc_action_level"
        )
        stored = {**point.tags, **point.fields, "_time": point.timestamp}

        restarted = AirQualityPipeline()
        self.assertEqual(restarted.restore_active_events([stored]), 1)
        # A replayed sample from before the trigger cannot clear the event.
        restarted.process(reading(-5, voc_index=100))
        completed = restarted.process(reading(60, voc_index=120))

        event = next(
            item
            for item in completed.event_points
            if item.tags.get("event_type") == "voc_action_level"
        )
        self.assertEqual(event.fields["state"], "completed")
        self.assertEqual(event.fields["event_start"], "2026-07-21T12:00:00Z")
        self.assertEqual(event.fields["duration_seconds"], 60)

    def test_graceful_shutdown_snapshot_preserves_active_peak_and_count(self) -> None:
        original = AirQualityPipeline()
        original.process(reading(0, voc_index=160))
        original.process(reading(10, voc_index=180))
        point = next(
            item
            for item in original.flush_active_events(BASE + timedelta(seconds=20))
            if item.tags.get("event_type") == "voc_action_level"
        )

        restarted = AirQualityPipeline()
        restarted.restore_active_events([{**point.tags, **point.fields}])
        # Recovery replay through the persisted last observation is idempotent.
        restarted.process(reading(5, voc_index=170))
        restarted.process(reading(10, voc_index=180))
        completed = restarted.process(reading(60, voc_index=120))

        event = next(
            item
            for item in completed.event_points
            if item.tags.get("event_type") == "voc_action_level"
        )
        self.assertEqual(event.fields["peak_value"], 180)
        self.assertEqual(event.fields["sample_count"], 3)

    def test_invalid_state_emits_one_event_until_cleared(self) -> None:
        pipeline = AirQualityPipeline()
        first = pipeline.process(reading(0, pm25=None))
        second = pipeline.process(reading(5, pm25=None))
        cleared = pipeline.process(reading(10, pm25=5))

        self.assertEqual(len([p for p in first.event_points if p.tags.get("event_type") == "sensor_invalid"]), 1)
        self.assertFalse(second.event_points)
        event = next(p for p in cleared.event_points if p.tags.get("event_type") == "sensor_invalid")
        self.assertEqual(event.fields["state"], "completed")

    def test_nonzero_sen66_device_status_is_an_invalid_sample_event(self) -> None:
        pipeline = AirQualityPipeline()

        result = pipeline.process(reading(0, status_flags=1 << 5))

        self.assertEqual(
            result.event_points[0].tags["event_type"], "sensor_invalid"
        )
        fields = pipeline.flush_partial(BASE)[0].fields
        self.assertEqual(fields["valid_sample_count"], 0)
        self.assertEqual(fields["invalid_sample_count"], 1)

    def test_invalid_event_respects_cooldown_after_clearing(self) -> None:
        pipeline = AirQualityPipeline()
        pipeline.process(reading(0, pm25=None))
        pipeline.process(reading(5))

        during_cooldown = pipeline.process(reading(10, pm25=None))
        after_cooldown = pipeline.process(reading(70, pm25=None))

        self.assertFalse(during_cooldown.event_points)
        self.assertEqual(
            after_cooldown.event_points[0].tags["event_type"], "sensor_invalid"
        )


if __name__ == "__main__":
    unittest.main()
