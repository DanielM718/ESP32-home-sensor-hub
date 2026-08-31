from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import (
    AirQualitySettings,
    AppSettings,
    InfluxSettings,
    MonitoringExportSettings,
    MqttSettings,
)
from app.export_queries import (
    ExportPoint,
    InfluxExportQueryRepository,
    _downsample_points,
    air_quality_aggregate_export_flux,
    air_quality_raw_export_flux,
    environment_export_flux,
    preview_recent_flux,
    printer_telemetry_export_flux,
)
from app.export_worker import LONG_HEADER, ExportWorker, _bounded_chunk_seconds
from app.persistence import MonitoringExportStore
from app.web import create_app
from app.workflow_services import ExportService, csv_safe_text, download_filename
from app.workflows import (
    Source,
    WorkflowConflictError,
    WorkflowNotFoundError,
    iso_utc,
    parse_stored_time,
    unit_for_field,
)

BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, value: datetime = BASE) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class DashboardRepository:
    def latest(self) -> dict[str, Any]:
        return {
            "generated_at": iso_utc(BASE),
            "environment": [
                {
                    "id": "1",
                    "sensor_type": "environment",
                    "node_id": 1,
                    "available_fields": ["temperature_c", "humidity", "battery_mv"],
                },
                {
                    "id": "2",
                    "sensor_type": "environment",
                    "node_id": 2,
                    "available_fields": ["temperature_c", "humidity"],
                },
            ],
            "air_quality": [
                {
                    "id": "printer_room",
                    "sensor_type": "air_quality",
                    "location": "printer_room",
                    "available_fields": [
                        "temperature_c",
                        "humidity",
                        "co2",
                        "pm1",
                        "pm25",
                        "pm4",
                        "pm10",
                        "voc_index",
                        "nox_index",
                    ],
                }
            ],
            "printer": [
                {
                    "id": "x2d",
                    "source_id": "x2d",
                    "sensor_type": "printer",
                    "printer_id": "x2d",
                    "available_fields": [
                        "chamber_temperature_c",
                        "bed_temperature_c",
                        "online",
                    ],
                }
            ],
            "ams": [
                {
                    "id": "x2d/ams_1",
                    "source_id": "x2d/ams_1",
                    "sensor_type": "ams",
                    "printer_id": "x2d",
                    "ams_id": "ams_1",
                    "available_fields": [
                        "ams_humidity",
                        "ams_temperature_c",
                        "ams_drying",
                    ],
                },
                {
                    "id": "x2d/ams_2",
                    "source_id": "x2d/ams_2",
                    "sensor_type": "ams",
                    "printer_id": "x2d",
                    "ams_id": "ams_2",
                    "available_fields": ["ams_humidity"],
                },
            ],
        }

    def air_quality_context(self) -> dict[str, Any]:
        return {"locations": {}}

    def readings(self, query: Any) -> dict[str, Any]:
        return {
            "generated_at": iso_utc(BASE),
            "range": query.range_key,
            "window": query.window_every,
            "sensor_type": query.sensor_type,
            "series": [],
            "events": [],
        }

    def nodes(self, **_kwargs: Any) -> dict[str, Any]:
        return {"generated_at": iso_utc(BASE), "nodes": []}


class FakeExportQuery:
    def __init__(self, points: list[ExportPoint] | None = None) -> None:
        self.points = list(points or [])
        self.calls: list[dict[str, Any]] = []
        self.preview_calls: list[dict[str, Any]] = []
        self.cancel_callback: Any = None

    def query_source_type(self, **kwargs: Any):
        self.calls.append(kwargs)
        selected = {source.key for source in kwargs["sources"]}
        start = kwargs["start"]
        stop = kwargs["stop"]
        resolution = kwargs["resolution"]
        for point in self.points:
            point_time = parse_stored_time(point.timestamp_utc)
            if (
                point.sensor_type == kwargs["sensor_type"]
                and point.source_key in selected
                and point_time is not None
                and start <= point_time < stop
                and (
                    point.data_tier == resolution
                    or (
                        kwargs.get("use_stored_aggregate")
                        and resolution == "15m"
                        and point.data_tier == "stored_15m"
                    )
                )
            ):
                yield point
                if self.cancel_callback is not None:
                    callback = self.cancel_callback
                    self.cancel_callback = None
                    callback()

    def monitoring_preview(self, **kwargs: Any) -> dict[str, Any]:
        self.preview_calls.append(kwargs)
        return {
            "row_count": 3,
            "row_count_is_approximate": True,
            "row_count_kind": "selected measurement values",
            "first_sample_timestamp": iso_utc(BASE),
            "latest_sample_timestamp": iso_utc(BASE + timedelta(seconds=5)),
            "source_presence": [],
            "recent_samples": [{"field": "temperature_c", "value": 22.5}],
            "recent_sample_limit": kwargs["limit"],
            "warnings": [],
        }


class PivotRecord:
    def __init__(self, timestamp: datetime, values: dict[str, Any]) -> None:
        self.timestamp = timestamp
        self.values = values

    def get_time(self) -> datetime:
        return self.timestamp


def settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        log_level="INFO",
        node_stale_after_seconds=1800,
        mqtt=MqttSettings(
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=60,
            client_id="test",
            username="test",
            password="test",
            sensor_topic="home/sensors/+",
            air_topic="home/air/+",
            qos=1,
            max_payload_bytes=4096,
        ),
        influx=InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="test",
            read_token="test",
            live_bucket="environment_live",
        ),
        air_quality=AirQualitySettings(),
        monitoring_exports=MonitoringExportSettings(
            database_path=tmp_path / "monitoring.sqlite3",
            output_dir=tmp_path / "exports",
            raw_retention_seconds=72 * 3600,
            raw_chunk_seconds=3600,
            aggregate_chunk_seconds=86400,
            worker_poll_seconds=1,
            lease_seconds=300,
            heartbeat_seconds=30,
        ),
    )


@pytest.fixture
def system(tmp_path: Path):
    app_settings = settings(tmp_path)
    store = MonitoringExportStore(
        app_settings.monitoring_exports.database_path,
        app_settings.monitoring_exports.output_dir,
    )
    query = FakeExportQuery()
    clock = FakeClock()
    app = create_app(
        app_settings,
        repository=DashboardRepository(),
        monitoring_store=store,
        export_query_repository=query,
        clock=clock,
    )
    app.testing = True
    return app.test_client(), store, query, clock, app_settings


def monitoring_payload(**overrides: Any) -> dict[str, Any]:
    result = {
        "name": "ASA filtration test",
        "notes": "Printer and filter enabled",
        "duration_seconds": 60,
        "sources": [
            {"sensor_type": "environment", "node_id": 1},
            {"sensor_type": "air_quality", "location": "printer_room"},
        ],
        "fields": ["temperature_c", "humidity", "battery_mv", "co2", "pm25"],
        "resolution": "raw",
        "csv_format": "long",
    }
    result.update(overrides)
    return result


def export_payload(**overrides: Any) -> dict[str, Any]:
    result = {
        "name": "Historical analysis",
        "start_time": "2026-07-31T08:00:00-04:00",
        "end_time": "2026-07-31T10:00:00-04:00",
        "sources": [
            {"sensor_type": "environment", "node_id": 1},
            {"sensor_type": "environment", "node_id": 2},
            {"sensor_type": "air_quality", "location": "printer_room"},
        ],
        "fields": ["temperature_c", "humidity", "battery_mv", "co2", "pm25"],
        "resolution": "raw",
        "csv_format": "long",
    }
    result.update(overrides)
    return result


def point(
    seconds: int,
    *,
    sensor_type: str = "environment",
    source_id: str = "1",
    field: str = "temperature_c",
    value: float = 22.5,
    data_tier: str = "raw",
    printer_id: str | None = None,
    ams_id: str | None = None,
) -> ExportPoint:
    return ExportPoint(
        timestamp_utc=iso_utc(BASE + timedelta(seconds=seconds)),
        sensor_type=sensor_type,
        source_id=source_id,
        node_id=int(source_id) if sensor_type == "environment" else None,
        location=source_id if sensor_type == "air_quality" else None,
        field=field,
        value=value,
        unit=unit_for_field(field),
        data_tier=data_tier,
        printer_id=printer_id,
        ams_id=ams_id,
    )


def create_job(store: MonitoringExportStore, clock: FakeClock, payload: dict[str, Any]):
    service = ExportService(store, clock=clock)
    return service.create(payload), service


class TestMonitoringApi:
    def test_app_creation_and_repository_injection(self, system: Any) -> None:
        client, _store, query, _clock, _settings = system
        assert client.get("/api/health").status_code == 200
        assert (
            client.get("/api/workflows/options").json["raw_retention_seconds"] == 259200
        )
        options = client.get("/api/workflows/options").json
        assert [item["value"] for item in options["monitoring_resolutions"]] == [
            "raw",
            "1m",
            "5m",
            "15m",
            "1h",
        ]
        assert [item["name"] for item in options["source_types"]] == [
            "environment",
            "air_quality",
            "printer",
            "ams",
        ]
        fields = {item["name"]: item for item in options["fields"]}
        assert fields["ams_humidity"]["sensor_types"] == ["ams"]
        assert fields["ams_humidity"]["numeric_aggregation"] is True
        assert fields["ams_drying"]["numeric_aggregation"] is False
        assert fields["chamber_temperature_c"]["sensor_types"] == ["printer"]
        assert query.calls == []

    def test_valid_session_creation_uses_server_timestamps(self, system: Any) -> None:
        client, _store, _query, _clock, _settings = system
        response = client.post("/api/monitoring/sessions", json=monitoring_payload())
        assert response.status_code == 201
        body = response.json
        assert body["status"] == "running"
        assert body["start_time_utc"] == iso_utc(BASE)
        assert body["scheduled_end_time_utc"] == iso_utc(BASE + timedelta(seconds=60))
        assert body["remaining_seconds"] == 60
        assert body["export"] is None

    def test_manual_ams_printer_and_mixed_monitoring_use_shared_capabilities(
        self, system: Any
    ) -> None:
        client = system[0]
        ams_only = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                name="AMS desiccant",
                sources=[
                    {
                        "sensor_type": "ams",
                        "printer_id": "x2d",
                        "ams_id": "ams_1",
                    }
                ],
                fields=["ams_humidity", "ams_temperature_c"],
            ),
        )
        printer_only = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                name="X2D chamber",
                sources=[{"sensor_type": "printer", "printer_id": "x2d"}],
                fields=["chamber_temperature_c"],
            ),
        )
        mixed = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                name="Mixed sources",
                sources=[
                    {"sensor_type": "environment", "node_id": 1},
                    {"sensor_type": "air_quality", "location": "printer_room"},
                    {"sensor_type": "printer", "printer_id": "x2d"},
                    {
                        "sensor_type": "ams",
                        "printer_id": "x2d",
                        "ams_id": "ams_1",
                    },
                ],
                fields=[
                    "temperature_c",
                    "humidity",
                    "co2",
                    "chamber_temperature_c",
                    "ams_humidity",
                    "ams_temperature_c",
                ],
            ),
        )

        assert ams_only.status_code == 201
        assert ams_only.json["selected_sources"] == [
            {"sensor_type": "ams", "printer_id": "x2d", "ams_id": "ams_1"}
        ]
        assert printer_only.status_code == 201
        assert mixed.status_code == 201

    def test_invalid_or_unsupported_bambu_selections_are_rejected(
        self, system: Any
    ) -> None:
        client = system[0]
        unknown = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[
                    {
                        "sensor_type": "ams",
                        "printer_id": "x2d",
                        "ams_id": "ams_99",
                    }
                ],
                fields=["ams_humidity"],
            ),
        )
        unsupported = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[{"sensor_type": "printer", "printer_id": "x2d"}],
                fields=["ams_humidity"],
            ),
        )
        averaged_boolean = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[{"sensor_type": "printer", "printer_id": "x2d"}],
                fields=["online"],
                resolution="5m",
            ),
        )
        mixed_boolean_mean = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[{"sensor_type": "printer", "printer_id": "x2d"}],
                fields=["chamber_temperature_c", "online"],
                resolution="5m",
            ),
        )

        assert unknown.status_code == 400
        assert unsupported.status_code == 400
        assert averaged_boolean.status_code == 400
        assert mixed_boolean_mean.status_code == 400

    @pytest.mark.parametrize("name", ["", "  ", 42, "x" * 121])
    def test_invalid_names(self, system: Any, name: Any) -> None:
        client = system[0]
        response = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(name=name)
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("duration", [9, 259201, True, "60"])
    def test_invalid_durations(self, system: Any, duration: Any) -> None:
        response = system[0].post(
            "/api/monitoring/sessions",
            json=monitoring_payload(duration_seconds=duration),
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "change",
        [
            {"sources": []},
            {"fields": []},
            {"sources": [{"sensor_type": "environment", "node_id": 0}]},
            {"sources": [{"sensor_type": "air_quality", "location": "bad room"}]},
            {"sources": [{"sensor_type": "unknown", "node_id": 1}]},
            {"fields": ["status_flags"]},
            {"resolution": "2m"},
        ],
    )
    def test_invalid_sources_fields_and_resolution(
        self, system: Any, change: Any
    ) -> None:
        response = system[0].post(
            "/api/monitoring/sessions", json=monitoring_payload(**change)
        )
        assert response.status_code == 400

    def test_newest_first_listing_and_retrieval(self, system: Any) -> None:
        client, _store, _query, clock, _settings = system
        first = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(name="one")
        ).json
        clock.advance(1)
        second = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(name="two")
        ).json
        listing = client.get("/api/monitoring/sessions").json["sessions"]
        assert [item["id"] for item in listing[:2]] == [second["id"], first["id"]]
        assert (
            client.get(f"/api/monitoring/sessions/{first['id']}").json["name"] == "one"
        )

    @pytest.mark.parametrize("resolution", ["raw", "1m", "5m", "15m", "1h"])
    def test_every_advertised_monitoring_resolution_is_accepted(
        self, system: Any, resolution: str
    ) -> None:
        response = system[0].post(
            "/api/monitoring/sessions",
            json=monitoring_payload(resolution=resolution),
        )
        assert response.status_code == 201
        assert response.json["resolution"] == resolution

    def test_wide_is_the_api_default(self, system: Any) -> None:
        payload = monitoring_payload()
        payload.pop("csv_format")
        response = system[0].post("/api/monitoring/sessions", json=payload)
        assert response.status_code == 201
        assert response.json["csv_format"] == "wide"

    def test_custom_eight_hours_twenty_five_minutes_is_exact(self, system: Any) -> None:
        response = system[0].post(
            "/api/monitoring/sessions",
            json=monitoring_payload(duration_seconds=8 * 3600 + 25 * 60),
        )
        assert response.status_code == 201
        assert response.json["duration_seconds"] == 30_300
        assert response.json["scheduled_end_time_utc"] == iso_utc(
            BASE + timedelta(seconds=30_300)
        )

    def test_schema_v1_migration_preserves_sessions_and_accepts_new_resolutions(
        self, system: Any
    ) -> None:
        client, store, _query, _clock, _settings = system
        original = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(resolution="raw")
        ).json
        with sqlite3.connect(store.database_path) as connection:
            connection.execute("PRAGMA user_version = 1")

        store.initialize()

        assert store.get_session(original["id"])["name"] == original["name"]
        created = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(resolution="5m")
        )
        assert created.status_code == 201

    def test_capability_validation_distinguishes_battery_per_node(
        self, system: Any
    ) -> None:
        client = system[0]
        without_battery = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[{"sensor_type": "environment", "node_id": 2}],
                fields=["battery_mv"],
            ),
        )
        with_battery = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["battery_mv"],
            ),
        )
        assert without_battery.status_code == 400
        assert "not available" in without_battery.json["message"]
        assert with_battery.status_code == 201

    def test_automatic_completion_and_exactly_one_export(self, system: Any) -> None:
        client, store, _query, clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions",
            json=monitoring_payload(duration_seconds=10),
        ).json
        clock.advance(11)
        for _index in range(4):
            body = client.get(f"/api/monitoring/sessions/{session['id']}").json
            assert body["status"] == "completed"
            assert body["effective_end_time_utc"] == body["scheduled_end_time_utc"]
        jobs = store.list_exports()
        assert len(jobs) == 1
        assert jobs[0]["monitoring_session_id"] == session["id"]

    def test_early_stop_is_idempotent_and_effective_end_is_server_now(
        self, system: Any
    ) -> None:
        client, store, _query, clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload()
        ).json
        clock.advance(17)
        stopped = client.post(f"/api/monitoring/sessions/{session['id']}/stop").json
        repeated = client.post(f"/api/monitoring/sessions/{session['id']}/stop").json
        assert stopped["status"] == "stopped"
        assert stopped["actual_end_time_utc"] == iso_utc(clock())
        assert repeated["actual_end_time_utc"] == stopped["actual_end_time_utc"]
        assert stopped["elapsed_seconds"] == 17
        assert len(store.list_exports()) == 1

    def test_stop_after_deadline_completes_at_scheduled_end(self, system: Any) -> None:
        client, _store, _query, clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload(duration_seconds=10)
        ).json
        clock.advance(20)
        stopped = client.post(f"/api/monitoring/sessions/{session['id']}/stop").json
        assert stopped["status"] == "completed"
        assert stopped["actual_end_time_utc"] == session["scheduled_end_time_utc"]

    def test_delete_conflict_while_running(self, system: Any) -> None:
        client = system[0]
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload()
        ).json
        response = client.delete(f"/api/monitoring/sessions/{session['id']}")
        assert response.status_code == 409

    def test_delete_stopped_session_cleans_queued_export(self, system: Any) -> None:
        client, store, _query, _clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload()
        ).json
        client.post(f"/api/monitoring/sessions/{session['id']}/stop")
        response = client.delete(f"/api/monitoring/sessions/{session['id']}")
        assert response.status_code == 204
        assert store.list_exports() == []

    def test_persistence_and_timing_across_app_recreation(self, tmp_path: Path) -> None:
        app_settings = settings(tmp_path)
        clock = FakeClock()
        store = MonitoringExportStore(
            app_settings.monitoring_exports.database_path,
            app_settings.monitoring_exports.output_dir,
        )
        first = create_app(
            app_settings,
            repository=DashboardRepository(),
            monitoring_store=store,
            export_query_repository=FakeExportQuery(),
            clock=clock,
        ).test_client()
        session = first.post("/api/monitoring/sessions", json=monitoring_payload()).json
        clock.advance(20)
        recreated_store = MonitoringExportStore(store.database_path, store.output_dir)
        second = create_app(
            app_settings,
            repository=DashboardRepository(),
            monitoring_store=recreated_store,
            export_query_repository=FakeExportQuery(),
            clock=clock,
        ).test_client()
        restored = second.get(f"/api/monitoring/sessions/{session['id']}").json
        assert restored["start_time_utc"] == session["start_time_utc"]
        assert restored["elapsed_seconds"] == 20
        assert restored["remaining_seconds"] == 40

    def test_preview_is_bounded(self, system: Any) -> None:
        client, _store, query, clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload()
        ).json
        clock.advance(5)
        preview = client.get(f"/api/monitoring/sessions/{session['id']}/preview").json
        assert preview["preview"]["row_count"] == 3
        assert query.preview_calls[0]["limit"] == 20
        assert query.preview_calls[0]["stop"] == clock()


class TestExportApi:
    def test_valid_job_returns_202_and_supports_arbitrary_mixed_sources(
        self, system: Any
    ) -> None:
        response = system[0].post("/api/exports", json=export_payload())
        assert response.status_code == 202
        assert response.json["status"] == "queued"
        assert response.json["source_count"] == 3
        assert response.json["selected_sources"][1]["node_id"] == 2

    @pytest.mark.parametrize(
        "change",
        [
            {"start_time": "bad"},
            {"end_time": "bad"},
            {"start_time": "2026-08-01T10:00:00"},
            {"end_time": "2026-08-01T10:00:00"},
            {
                "start_time": "2026-08-01T10:00:00Z",
                "end_time": "2026-08-01T10:00:00Z",
            },
            {
                "start_time": "2026-08-01T11:00:00Z",
                "end_time": "2026-08-01T10:00:00Z",
            },
        ],
    )
    def test_invalid_time_inputs(self, system: Any, change: Any) -> None:
        response = system[0].post("/api/exports", json=export_payload(**change))
        assert response.status_code == 400

    def test_utc_normalization_is_exact(self, system: Any) -> None:
        body = system[0].post("/api/exports", json=export_payload()).json
        assert body["start_time_utc"] == "2026-07-31T12:00:00.000000Z"
        assert body["end_time_utc"] == "2026-07-31T14:00:00.000000Z"

    def test_wide_is_the_historical_export_api_default(self, system: Any) -> None:
        payload = export_payload()
        payload.pop("csv_format")
        response = system[0].post("/api/exports", json=payload)
        assert response.status_code == 202
        assert response.json["csv_format"] == "wide"

    def test_raw_retention_warning_without_aggregate_substitution(
        self, system: Any
    ) -> None:
        body = (
            system[0]
            .post(
                "/api/exports",
                json=export_payload(
                    start_time="2026-07-01T00:00:00Z",
                    end_time="2026-07-02T00:00:00Z",
                ),
            )
            .json
        )
        assert any("may have expired" in warning for warning in body["warnings"])
        assert body["resolution"] == "raw"

    def test_15m_environment_only_is_rejected_but_mixed_warns(
        self, system: Any
    ) -> None:
        client = system[0]
        rejected = client.post(
            "/api/exports",
            json=export_payload(
                resolution="15m",
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["temperature_c"],
            ),
        )
        assert rejected.status_code == 400
        accepted = client.post("/api/exports", json=export_payload(resolution="15m"))
        assert accepted.status_code == 202
        assert any("zero rows" in warning for warning in accepted.json["warnings"])

    def test_job_persists_across_store_recreation(self, system: Any) -> None:
        client, store, _query, _clock, _settings = system
        job = client.post("/api/exports", json=export_payload()).json
        recreated = MonitoringExportStore(store.database_path, store.output_dir)
        recreated.initialize()
        assert recreated.get_export(job["id"])["name"] == job["name"]

    def test_transactional_claim_and_no_double_claim(self, system: Any) -> None:
        client, store, _query, clock, _settings = system
        job = client.post("/api/exports", json=export_payload()).json
        first = store.claim_oldest_export(worker_id="worker-1", now=clock())
        second = store.claim_oldest_export(worker_id="worker-2", now=clock())
        assert first["id"] == job["id"]
        assert second is None
        assert store.get_export(job["id"])["attempt_count"] == 1

    def test_only_one_heavy_job_is_claimed_across_workers(self, system: Any) -> None:
        client, store, _query, clock, _settings = system
        first_job = client.post("/api/exports", json=export_payload(name="first")).json
        client.post("/api/exports", json=export_payload(name="second"))

        first_claim = store.claim_oldest_export(worker_id="worker-1", now=clock())
        other_worker_claim = store.claim_oldest_export(
            worker_id="worker-2", now=clock()
        )

        assert first_claim["id"] == first_job["id"]
        assert other_worker_claim is None

    def test_stale_running_recovery_and_partial_cleanup(self, system: Any) -> None:
        client, store, query, clock, _settings = system
        job = client.post("/api/exports", json=export_payload()).json
        store.claim_oldest_export(worker_id="dead-worker", now=clock())
        final = store.output_dir / f"{job['id']}.csv"
        partial = store.output_dir / f"{job['id']}.csv.part"
        final.write_text("orphan final", encoding="utf-8")
        partial.write_text("partial", encoding="utf-8")
        clock.advance(301)
        worker = ExportWorker(
            store,
            query,
            clock=clock,
            worker_id="recovery-worker",
            start_heartbeat_thread=False,
        )
        recovered = worker.recover()
        assert recovered == [{"id": job["id"], "status": "queued"}]
        assert not final.exists()
        assert not partial.exists()

    def test_cancel_queued_is_immediate_and_idempotent(self, system: Any) -> None:
        client = system[0]
        job = client.post("/api/exports", json=export_payload()).json
        first = client.post(f"/api/exports/{job['id']}/cancel")
        second = client.post(f"/api/exports/{job['id']}/cancel")
        assert first.status_code == 200
        assert first.json["status"] == "cancelled"
        assert second.json["status"] == "cancelled"

    def test_running_cancel_is_cooperative(self, system: Any) -> None:
        client, store, query, clock, _settings = system
        query.points = [point(-3600), point(-3595)]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["temperature_c"],
            ),
        ).json
        query.cancel_callback = lambda: store.request_cancel(job["id"], now=clock())
        worker = ExportWorker(
            store,
            query,
            clock=clock,
            worker_id="worker",
            start_heartbeat_thread=False,
        )
        assert worker.run_once()
        assert store.get_export(job["id"])["status"] == "cancelled"
        assert not (store.output_dir / f"{job['id']}.csv").exists()
        assert not (store.output_dir / f"{job['id']}.csv.part").exists()

    def test_download_unavailable_before_completion(self, system: Any) -> None:
        client = system[0]
        job = client.post("/api/exports", json=export_payload()).json
        assert client.get(f"/api/exports/{job['id']}/download").status_code == 409

    def test_delete_cancelled_removes_files_and_metadata(self, system: Any) -> None:
        client, store, _query, _clock, _settings = system
        job = client.post("/api/exports", json=export_payload()).json
        client.post(f"/api/exports/{job['id']}/cancel")
        final = store.output_dir / f"{job['id']}.csv"
        partial = store.output_dir / f"{job['id']}.csv.part"
        final.write_text("stale", encoding="utf-8")
        partial.write_text("stale", encoding="utf-8")
        assert client.delete(f"/api/exports/{job['id']}").status_code == 204
        assert not final.exists() and not partial.exists()
        with pytest.raises(WorkflowNotFoundError):
            store.get_export(job["id"])

    def test_uuid_and_path_traversal_resistance(self, system: Any) -> None:
        client, store, _query, clock, _settings = system
        assert client.get("/api/exports/not-a-uuid").status_code == 404
        service = ExportService(store, clock=clock)
        with pytest.raises(WorkflowConflictError):
            service.safe_paths("../../outside")


class TestExportWorkerAndCsv:
    def test_chunk_size_shrinks_to_bound_large_air_quality_selections(self) -> None:
        sources = [
            Source("air_quality", location=f"station_{index}") for index in range(100)
        ]
        fields = [
            "temperature_c",
            "humidity",
            "co2",
            "pm1",
            "pm25",
            "pm4",
            "pm10",
            "voc_index",
            "nox_index",
        ]
        assert _bounded_chunk_seconds("raw", 3600, sources, fields) == 300
        assert _bounded_chunk_seconds("15m", 86400, sources, fields) < 86400

    def test_long_csv_mixed_sources_is_deterministic_and_downloadable(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [
            point(
                -3590,
                sensor_type="air_quality",
                source_id="printer_room",
                field="humidity",
                value=40,
            ),
            point(
                -3600,
                sensor_type="environment",
                source_id="1",
                field="temperature_c",
                value=21,
            ),
            point(
                -3590,
                sensor_type="air_quality",
                source_id="printer_room",
                field="temperature_c",
                value=23,
            ),
        ]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[
                    {"sensor_type": "environment", "node_id": 1},
                    {"sensor_type": "air_quality", "location": "printer_room"},
                ],
                fields=["temperature_c", "humidity"],
            ),
        ).json
        worker = ExportWorker(
            store,
            query,
            clock=clock,
            worker_id="worker",
            start_heartbeat_thread=False,
        )
        assert worker.run_once()
        completed = store.get_export(job["id"])
        assert completed["status"] == "completed"
        assert completed["rows_written"] == 3
        path = store.output_dir / f"{job['id']}.csv"
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert tuple(rows[0]) == LONG_HEADER
        assert rows[1][0] < rows[2][0]
        assert [row[5] for row in rows[2:]] == ["humidity", "temperature_c"]
        download = client.get(f"/api/exports/{job['id']}/download")
        assert download.status_code == 200
        assert download.mimetype == "text/csv"
        assert "attachment" in download.headers["Content-Disposition"]
        assert download.data.startswith(",".join(LONG_HEADER).encode("utf-8"))

    def test_zero_data_completes_with_header_only_and_source_results(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        job = client.post("/api/exports", json=export_payload()).json
        worker = ExportWorker(
            store,
            query,
            clock=clock,
            worker_id="worker",
            start_heartbeat_thread=False,
        )
        worker.run_once()
        completed = store.get_export(job["id"])
        assert completed["status"] == "completed"
        assert completed["rows_written"] == 0
        assert all(
            item["status"] == "zero_data" for item in completed["source_results"]
        )
        content = (store.output_dir / f"{job['id']}.csv").read_text(encoding="utf-8")
        assert content == ",".join(LONG_HEADER) + "\n"

    def test_immediate_monitoring_stop_exports_header_only(self, system: Any) -> None:
        client, store, query, clock, _settings = system
        session = client.post(
            "/api/monitoring/sessions", json=monitoring_payload()
        ).json
        stopped = client.post(f"/api/monitoring/sessions/{session['id']}/stop").json
        worker = ExportWorker(
            store,
            query,
            clock=clock,
            worker_id="worker",
            start_heartbeat_thread=False,
        )

        worker.run_once()

        completed = store.get_export(stopped["export"]["id"])
        assert completed["status"] == "completed"
        assert completed["rows_written"] == 0
        assert completed["work_units_total"] == 0
        content = (store.output_dir / f"{completed['id']}.csv").read_text(
            encoding="utf-8"
        )
        assert content == ",".join(LONG_HEADER) + "\n"

    def test_partial_source_coverage_succeeds(self, system: Any) -> None:
        client, store, query, clock, _settings = system
        query.points = [point(-3600, source_id="1")]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[
                    {"sensor_type": "environment", "node_id": 1},
                    {"sensor_type": "environment", "node_id": 2},
                ],
                fields=["temperature_c"],
            ),
        ).json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        worker.run_once()
        results = {
            item["source_id"]: item
            for item in store.get_export(job["id"])["source_results"]
        }
        assert results["1"]["status"] == "data"
        assert results["2"]["status"] == "zero_data"

    def test_chunk_boundaries_have_no_duplication_or_omission(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [point(-7200), point(-3600), point(-1)]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["temperature_c"],
            ),
        ).json
        worker = ExportWorker(
            store,
            query,
            raw_chunk_seconds=3600,
            clock=clock,
            worker_id="worker",
            start_heartbeat_thread=False,
        )
        worker.run_once()
        completed = store.get_export(job["id"])
        assert completed["rows_written"] == 3
        assert completed["work_units_total"] == 2
        assert completed["work_units_completed"] == 2
        assert len(query.calls) == 2

    @pytest.mark.parametrize("csv_format", ["long", "wide"])
    def test_mixed_bambu_environment_csv_has_explicit_stable_identities(
        self, system: Any, csv_format: str
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [
            point(-3600, source_id="1", field="temperature_c", value=21.0),
            point(
                -3600,
                sensor_type="air_quality",
                source_id="printer_room",
                field="co2",
                value=650,
            ),
            point(
                -3600,
                sensor_type="printer",
                source_id="x2d",
                field="chamber_temperature_c",
                value=31.5,
                printer_id="x2d",
            ),
            point(
                -3600,
                sensor_type="ams",
                source_id="x2d/ams_1",
                field="ams_humidity",
                value=22.0,
                printer_id="x2d",
                ams_id="ams_1",
            ),
            point(
                -3600,
                sensor_type="ams",
                source_id="x2d/ams_1",
                field="ams_temperature_c",
                value=25.0,
                printer_id="x2d",
                ams_id="ams_1",
            ),
        ]
        response = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[
                    {"sensor_type": "environment", "node_id": 1},
                    {"sensor_type": "air_quality", "location": "printer_room"},
                    {"sensor_type": "printer", "printer_id": "x2d"},
                    {
                        "sensor_type": "ams",
                        "printer_id": "x2d",
                        "ams_id": "ams_1",
                    },
                ],
                fields=[
                    "temperature_c",
                    "co2",
                    "chamber_temperature_c",
                    "ams_humidity",
                    "ams_temperature_c",
                ],
                csv_format=csv_format,
            ),
        )
        assert response.status_code == 202
        job = response.json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        assert worker.run_once()
        with (store.output_dir / f"{job['id']}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        assert "printer_id" in reader.fieldnames and "ams_id" in reader.fieldnames
        ams_rows = [row for row in rows if row["source_id"] == "x2d/ams_1"]
        assert ams_rows
        assert {row["printer_id"] for row in ams_rows} == {"x2d"}
        assert {row["ams_id"] for row in ams_rows} == {"ams_1"}
        if csv_format == "long":
            units = {row["field"]: row["unit"] for row in ams_rows}
            assert units == {
                "ams_humidity": "percent",
                "ams_temperature_c": "degC",
            }
        else:
            assert ams_rows[0]["ams_humidity"] == "22.0"
            assert ams_rows[0]["ams_temperature_c"] == "25.0"

    def test_wide_csv_has_blank_unavailable_fields(self, system: Any) -> None:
        client, store, query, clock, _settings = system
        query.points = [point(-3600, field="temperature_c", value=22.0)]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["temperature_c", "humidity"],
                csv_format="wide",
            ),
        ).json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        worker.run_once()
        with (store.output_dir / f"{job['id']}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            rows = list(csv.DictReader(handle))
        assert len(rows) == 1
        assert rows[0]["temperature_c"] == "22.0"
        assert rows[0]["humidity"] == ""
        assert rows[0]["data_tier"] == "raw"

    def test_wide_csv_puts_fields_on_same_row_and_keeps_sources_distinct(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [
            point(-3600, source_id="1", field="temperature_c", value=22.0),
            point(-3600, source_id="1", field="humidity", value=41.5),
            point(-3600, source_id="2", field="temperature_c", value=24.0),
        ]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[
                    {"sensor_type": "environment", "node_id": 1},
                    {"sensor_type": "environment", "node_id": 2},
                ],
                fields=["temperature_c", "humidity"],
                csv_format="wide",
            ),
        ).json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        assert worker.run_once()
        with (store.output_dir / f"{job['id']}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            assert reader.fieldnames == [
                "timestamp_utc",
                "sensor_type",
                "source_id",
                "node_id",
                "location",
                "temperature_c",
                "humidity",
                "data_tier",
            ]
        assert len(rows) == 2
        by_source = {row["source_id"]: row for row in rows}
        assert by_source["1"]["temperature_c"] == "22.0"
        assert by_source["1"]["humidity"] == "41.5"
        assert by_source["2"]["temperature_c"] == "24.0"
        assert by_source["2"]["humidity"] == ""

    def test_aggregate_csv_uses_normal_measurement_name_and_tier(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [
            point(
                -86400,
                sensor_type="air_quality",
                source_id="printer_room",
                field="temperature_c",
                value=22.25,
                data_tier="stored_15m",
            )
        ]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(days=2)),
                end_time=iso_utc(BASE),
                sources=[{"sensor_type": "air_quality", "location": "printer_room"}],
                fields=["temperature_c"],
                resolution="15m",
            ),
        ).json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        worker.run_once()
        with (store.output_dir / f"{job['id']}.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["field"] == "temperature_c"
        assert rows[0]["data_tier"] == "stored_15m"

    def test_worker_graceful_stop_requeues_without_final_file(
        self, system: Any
    ) -> None:
        client, store, query, clock, _settings = system
        query.points = [point(-3600)]
        job = client.post(
            "/api/exports",
            json=export_payload(
                start_time=iso_utc(BASE - timedelta(hours=2)),
                end_time=iso_utc(BASE),
                sources=[{"sensor_type": "environment", "node_id": 1}],
                fields=["temperature_c"],
            ),
        ).json
        worker = ExportWorker(
            store, query, clock=clock, worker_id="worker", start_heartbeat_thread=False
        )
        worker.stop()
        worker.run_once()
        assert store.get_export(job["id"])["status"] == "queued"
        assert not (store.output_dir / f"{job['id']}.csv").exists()

    def test_formula_injection_and_filename_sanitization(self) -> None:
        assert csv_safe_text("=SUM(A1:A2)") == "'=SUM(A1:A2)"
        assert csv_safe_text("+cmd") == "'+cmd"
        assert csv_safe_text("normal") == "normal"
        filename = download_filename(
            {
                "name": "../../ = ASA test",
                "start_time_utc": iso_utc(BASE),
                "id": "a1b2c3d4-0000-0000-0000-000000000000",
            }
        )
        assert filename == "asa-test_2026-08-01T120000Z_a1b2c3d4.csv"
        assert "/" not in filename


class TestExportFlux:
    @pytest.mark.parametrize(
        ("resolution", "second_offset"),
        [("1m", 30), ("5m", 240), ("15m", 840), ("1h", 3500)],
    )
    def test_downsampled_resolutions_calculate_means(
        self, resolution: str, second_offset: int
    ) -> None:
        points = [
            point(0, field="temperature_c", value=20.0),
            point(second_offset, field="temperature_c", value=24.0),
            point(0, field="humidity", value=40.0),
            point(second_offset, field="humidity", value=44.0),
        ]
        result = list(_downsample_points(points, start=BASE, resolution=resolution))
        assert [(item.field, item.value) for item in result] == [
            ("humidity", 42.0),
            ("temperature_c", 22.0),
        ]
        assert {item.timestamp_utc for item in result} == {iso_utc(BASE)}
        assert {item.data_tier for item in result} == {f"{resolution}_mean"}

    def test_query_repository_downsamples_raw_and_averages_only_valid_battery(
        self,
    ) -> None:
        records = [
            PivotRecord(
                BASE,
                {
                    "node_id": "1",
                    "temperature_c": 20.0,
                    "battery_mv": 4000,
                    "status_flags": 4,
                },
            ),
            PivotRecord(
                BASE + timedelta(seconds=30),
                {
                    "node_id": "1",
                    "temperature_c": 24.0,
                    "battery_mv": 1000,
                    "status_flags": 0,
                },
            ),
        ]

        class QueryApi:
            def query_stream(self, **_kwargs: Any):
                return iter(records)

        repository = InfluxExportQueryRepository(
            SimpleNamespace(
                bucket="environment",
                live_bucket="environment_live",
                org="home",
            ),
            query_api=QueryApi(),
        )
        result = list(
            repository.query_source_type(
                sensor_type="environment",
                start=BASE,
                stop=BASE + timedelta(minutes=1),
                sources=[Source("environment", node_id=1)],
                fields=["temperature_c", "battery_mv"],
                resolution="1m",
            )
        )

        assert {item.field: item.value for item in result} == {
            "battery_mv": 4000.0,
            "temperature_c": 22.0,
        }
        assert {item.data_tier for item in result} == {"1m_mean"}

    def test_exact_start_stop_environment_bucket_and_allowlisted_fields(self) -> None:
        start = BASE
        stop = BASE + timedelta(hours=1)
        flux = environment_export_flux(
            "environment",
            start,
            stop,
            [Source("environment", node_id=1)],
            ["temperature_c", "battery_mv"],
        )
        assert 'from(bucket: "environment")' in flux
        assert (
            f'range(start: time(v: "{iso_utc(start)}"), stop: time(v: "{iso_utc(stop)}"))'
            in flux
        )
        assert 'r._measurement == "environment_reading"' in flux
        assert 'set: ["temperature_c", "battery_mv", "status_flags"]' in flux
        assert 'set: ["1"]' in flux

    def test_raw_air_uses_live_bucket_and_no_aggregate(self) -> None:
        flux = air_quality_raw_export_flux(
            "environment_live",
            BASE,
            BASE + timedelta(hours=1),
            [Source("air_quality", location="office")],
            ["co2", "pm25"],
        )
        assert 'from(bucket: "environment_live")' in flux
        assert 'r._measurement == "air_quality_reading"' in flux
        assert "air_quality_15m" not in flux
        assert '"sample_valid"' in flux

    def test_aggregate_uses_actual_measurement_and_mean_fields(self) -> None:
        flux = air_quality_aggregate_export_flux(
            "environment",
            BASE,
            BASE + timedelta(days=1),
            [Source("air_quality", location="office")],
            ["co2", "pm25"],
        )
        assert 'from(bucket: "environment")' in flux
        assert 'r._measurement == "air_quality_15m"' in flux
        assert 'set: ["co2_mean", "pm25_mean"]' in flux
        assert "air_quality_reading" not in flux

    def test_printer_flux_uses_stable_source_filters_and_structured_fields(
        self,
    ) -> None:
        flux = printer_telemetry_export_flux(
            "environment_live",
            BASE,
            BASE + timedelta(hours=1),
            [
                Source("printer", printer_id="x2d"),
                Source("ams", printer_id="x2d", ams_id="ams_1"),
                Source("ams", printer_id="x2d", ams_id="ams_2"),
            ],
            ["chamber_temperature_c", "ams_humidity"],
        )

        assert 'r._measurement == "printer_telemetry"' in flux
        assert 'r.printer_id == "x2d"' in flux
        assert 'r.component_type == "printer"' in flux
        assert 'r.component_id == "main"' in flux
        assert 'r.component_type == "ams"' in flux
        assert 'r.component_id == "ams_1"' in flux
        assert 'r.component_id == "ams_2"' in flux
        assert 'set: ["chamber_temperature_c", "ams_humidity"]' in flux
        assert "ams_inventory_json" not in flux

    def test_bambu_preview_keeps_field_tables_separate_for_mixed_types(self) -> None:
        flux = preview_recent_flux(
            "environment_live",
            "ams",
            BASE,
            BASE + timedelta(hours=1),
            [Source("ams", printer_id="x2d", ams_id="ams_1")],
            ["ams_humidity", "ams_drying"],
            limit=20,
        )

        assert 'set: ["ams_humidity", "ams_drying"]' in flux
        assert "|> group()" not in flux
        assert "|> limit(n: 20)" in flux

    def test_raw_bambu_export_preserves_numeric_boolean_and_ams_identity(
        self,
    ) -> None:
        records = [
            PivotRecord(
                BASE,
                {
                    "printer_id": "x2d",
                    "component_type": "ams",
                    "component_id": "ams_1",
                    "ams_humidity": 22.0,
                    "ams_temperature_c": 25.5,
                    "ams_drying": True,
                },
            )
        ]

        class QueryApi:
            def query_stream(self, **_kwargs: Any):
                return iter(records)

        repository = InfluxExportQueryRepository(
            SimpleNamespace(
                bucket="environment",
                live_bucket="environment_live",
                org="home",
            ),
            query_api=QueryApi(),
        )
        result = list(
            repository.query_source_type(
                sensor_type="ams",
                start=BASE,
                stop=BASE + timedelta(minutes=1),
                sources=[Source("ams", printer_id="x2d", ams_id="ams_1")],
                fields=["ams_humidity", "ams_temperature_c", "ams_drying"],
                resolution="raw",
            )
        )

        assert {item.field: item.value for item in result} == {
            "ams_humidity": 22.0,
            "ams_temperature_c": 25.5,
            "ams_drying": True,
        }
        assert {item.source_id for item in result} == {"x2d/ams_1"}
        assert {item.printer_id for item in result} == {"x2d"}
        assert {item.ams_id for item in result} == {"ams_1"}
        assert {item.data_tier for item in result} == {"live_raw"}

    def test_boolean_bambu_field_is_raw_only(self) -> None:
        class QueryApi:
            calls = 0

            def query_stream(self, **_kwargs: Any):
                self.calls += 1
                return iter(())

        query_api = QueryApi()
        repository = InfluxExportQueryRepository(
            SimpleNamespace(
                bucket="environment",
                live_bucket="environment_live",
                org="home",
            ),
            query_api=query_api,
        )
        result = list(
            repository.query_source_type(
                sensor_type="printer",
                start=BASE,
                stop=BASE + timedelta(minutes=5),
                sources=[Source("printer", printer_id="x2d")],
                fields=["online"],
                resolution="5m",
            )
        )

        assert result == []
        assert query_api.calls == 0

    def test_old_bambu_downsampling_uses_durable_tier_and_labels_it(self) -> None:
        records = [
            PivotRecord(
                BASE - timedelta(days=5),
                {
                    "printer_id": "x2d",
                    "component_type": "ams",
                    "component_id": "ams_1",
                    "ams_humidity": 22.0,
                },
            )
        ]

        class QueryApi:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def query_stream(self, **kwargs: Any):
                self.queries.append(kwargs["query"])
                return iter(records)

        query_api = QueryApi()
        repository = InfluxExportQueryRepository(
            SimpleNamespace(
                bucket="environment",
                live_bucket="environment_live",
                org="home",
            ),
            query_api=query_api,
            raw_retention_seconds=72 * 3600,
            clock=lambda: BASE,
        )
        result = list(
            repository.query_source_type(
                sensor_type="ams",
                start=BASE - timedelta(days=7),
                stop=BASE - timedelta(days=4),
                sources=[Source("ams", printer_id="x2d", ams_id="ams_1")],
                fields=["ams_humidity"],
                resolution="5m",
            )
        )

        assert len(result) == 1
        assert result[0].value == 22.0
        assert result[0].data_tier == "5m_mean_from_durable_5m"
        assert any(
            'from(bucket: "environment")' in query for query in query_api.queries
        )
        assert not any(
            'from(bucket: "environment_live")' in query for query in query_api.queries
        )
