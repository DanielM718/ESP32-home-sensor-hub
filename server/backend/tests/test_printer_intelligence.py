from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.persistence import MonitoringExportStore
from app.printer_adapter import printer_state_from_home_assistant
from app.printer_config import (
    AmsObserverSettings,
    MaintenanceTaskSettings,
    PrinterObserverSettings,
)
from app.printer_intelligence import BambuCloudHistoryAdapter, PrinterIntelligenceStore
from app.printer_model import NormalizedPrinterState, PrinterState
from app.printer_monitoring import PrinterMonitoringCoordinator
from app.printer_persistence import PrinterStore
from app.workflows import MonitoringRequest, Source

NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


class _CloudResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return json.dumps(self.payload).encode()


def _state(
    when: datetime,
    normalized: NormalizedPrinterState,
    *,
    job_id: str | None = "job-1",
    job_name: str = "Calibration cube",
) -> PrinterState:
    return PrinterState(
        printer_id="x2d",
        printer_model="X2D",
        online=True,
        normalized_state=normalized,
        source="home_assistant",
        source_timestamp=when,
        observed_at=when,
        job_id=job_id,
        job_name=job_name,
    )


def _finish(
    store: PrinterStore,
    *,
    start: datetime,
    hours: int,
    job_id: str,
    job_name: str = "Calibration cube",
) -> None:
    store.process(
        _state(
            start,
            NormalizedPrinterState.PRINTING,
            job_id=job_id,
            job_name=job_name,
        )
    )
    end = start + timedelta(hours=hours)
    store.process(
        _state(
            end,
            NormalizedPrinterState.COMPLETED,
            job_id=job_id,
            job_name=job_name,
        )
    )
    store.process(
        _state(
            end + timedelta(seconds=15),
            NormalizedPrinterState.COMPLETED,
            job_id=job_id,
            job_name=job_name,
        )
    )


def _cloud(cloud_id: str = "42", **overrides):
    result = {
        "id": int(cloud_id),
        "title": "Calibration cube",
        "designTitle": "Cube project",
        "status": 2,
        "startTime": "2026-08-12T12:00:00Z",
        "endTime": "2026-08-12T14:00:15Z",
        "costTime": 7000,
        "weight": 12.5,
        "length": 420,
        "plateIndex": 1,
        "bedType": "supertack_plate",
        "mode": "cloud_file",
        "deviceModel": "X2D",
        "cover": "https://example.invalid/not-stored",
        "amsDetailMapping": [
            {
                "ams": 0,
                "slotId": 3,
                "nozzleId": 1,
                "filamentType": "PETG",
                "sourceColor": "FFFFFFFF",
            }
        ],
    }
    result.update(overrides)
    return result


def test_actual_x2d_dual_nozzle_ams2pro_and_hybrid_mapping(tmp_path: Path) -> None:
    prefix = "x2d_redacted"
    entities = {
        "online": f"binary_sensor.{prefix}_online",
        "print_status": f"sensor.{prefix}_print_status",
        "nozzle_1_temperature": f"sensor.{prefix}_left_nozzle_temperature",
        "nozzle_1_target": f"sensor.{prefix}_left_nozzle_target_temperature",
        "nozzle_2_temperature": f"sensor.{prefix}_right_nozzle_temperature",
        "nozzle_2_target": f"sensor.{prefix}_right_nozzle_target_temperature",
        "mqtt_connection_mode": f"sensor.{prefix}_mqtt_connection_mode",
        "mqtt_encryption": f"binary_sensor.{prefix}_mqtt_encryption",
        "developer_lan_mode": f"binary_sensor.{prefix}_developer_lan_mode",
        "ha_bambulab_estimated_usage_hours": f"sensor.{prefix}_total_usage",
        "ams_slot": f"sensor.{prefix}_active_tray",
    }
    ams = AmsObserverSettings(
        "ams_1",
        "AMS 2 Pro",
        entities={
            "active": f"binary_sensor.{prefix}_ams_1_active",
            "humidity_percent": f"sensor.{prefix}_ams_1_humidity",
            "temperature": f"sensor.{prefix}_ams_1_temperature",
            "drying": f"binary_sensor.{prefix}_ams_1_drying",
        },
        tray_entities=(f"sensor.{prefix}_ams_1_tray_1",),
    )
    settings = PrinterObserverSettings(
        "x2d",
        "X2D",
        "http://127.0.0.1:8123",
        entities,
        database_path=tmp_path / "printer.sqlite3",
        ams_units=(ams,),
    ).validated()

    def entity(value, attributes=None):
        return {
            "state": value,
            "attributes": attributes or {},
            "last_reported": NOW.isoformat(),
        }

    states = {
        entities["online"]: entity("on"),
        entities["print_status"]: entity("running"),
        entities["nozzle_1_temperature"]: entity("220", {"unit_of_measurement": "°C"}),
        entities["nozzle_1_target"]: entity("225"),
        entities["nozzle_2_temperature"]: entity("31"),
        entities["nozzle_2_target"]: entity("0"),
        entities["mqtt_connection_mode"]: entity("local"),
        entities["mqtt_encryption"]: entity("on"),
        entities["developer_lan_mode"]: entity("off"),
        entities["ha_bambulab_estimated_usage_hours"]: entity("0.0"),
        entities["ams_slot"]: entity(
            "Bambu PETG Basic", {"slot": 4, "type": "PETG", "name": "Bambu PETG Basic"}
        ),
        ams.entities["active"]: entity("on"),
        ams.entities["humidity_percent"]: entity("1"),
        ams.entities["temperature"]: entity("31.2"),
        ams.entities["drying"]: entity("off"),
        ams.tray_entities[0]: entity(
            "Bambu PLA Matte",
            {
                "slot": 1,
                "type": "PLA",
                "name": "Bambu PLA Matte",
                "color": "#FFFFFFFF",
                "remain": 43,
                "active": False,
                "empty": False,
            },
        ),
    }
    observed = printer_state_from_home_assistant(states, settings, observed_at=NOW)
    assert (observed.nozzle_1_temperature, observed.nozzle_2_temperature) == (220, 31)
    assert observed.mqtt_connection_mode == "local"
    assert observed.mqtt_encryption is True
    assert observed.developer_lan_mode is False
    assert observed.ha_bambulab_estimated_usage_hours == 0
    assert observed.printer_reported_lifetime_hours is None
    assert observed.ams_slot == "4"
    assert observed.ams_units[0].model == "AMS 2 Pro"
    assert observed.ams_units[0].trays[0].remaining_percent == 43


def test_cloud_import_is_idempotent_preserves_missing_times_and_reconciles_local(
    tmp_path: Path,
) -> None:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    _finish(local, start=NOW, hours=2, job_id="42")
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    records = [_cloud(), _cloud("43", startTime=None, endTime=None, status=3)]

    first = intelligence.import_cloud_records("x2d", records, imported_at=NOW)
    second = intelligence.import_cloud_records(
        "x2d", records, imported_at=NOW + timedelta(minutes=1)
    )

    assert first == {"inserted": 2, "updated": 0, "reconciled": 1}
    assert second == {"inserted": 0, "updated": 2, "reconciled": 1}
    history = intelligence.history("x2d")
    assert len(history) == 2
    reconciled = next(item for item in history if item["job_id"] == "42")
    assert reconciled["source"] == "locally_observed"
    assert reconciled["provenance"] == ["locally_observed", "bambu_cloud_history"]
    assert reconciled["result"] == "completed"
    missing = next(item for item in history if item["job_id"] == "43")
    assert missing["started_at"] is None and missing["ended_at"] is None
    assert missing["result"] == "aborted_or_failed"


def test_cloud_adapter_follows_short_pages_when_api_total_has_more_records() -> None:
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        after = parse_qs(urlparse(request.full_url).query).get("after")
        if after is None:
            return _CloudResponse({"total": 3, "hits": [_cloud("1"), _cloud("2")]})
        return _CloudResponse({"total": 3, "hits": [_cloud("3")]})

    result = BambuCloudHistoryAdapter(
        "token-marker",
        "device-marker",
        timeout_seconds=4,
        opener=opener,
    ).fetch()

    assert result["records_retrieved"] == 3
    assert result["truncated"] is False
    assert len(requests) == 2
    assert requests[0][1] == 4
    assert parse_qs(urlparse(requests[1][0].full_url).query)["after"] == ["2"]
    assert "token-marker" not in requests[0][0].full_url


def test_usage_high_water_avoids_cloud_and_ha_double_counting(tmp_path: Path) -> None:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    _finish(local, start=NOW, hours=2, job_id="one")
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    intelligence.import_cloud_records("x2d", [_cloud()], imported_at=NOW)
    intelligence.observe_usage(
        "x2d",
        observed_at=NOW + timedelta(hours=3),
        ha_estimate_hours=100,
        printer_reported_hours=None,
    )
    _finish(local, start=NOW + timedelta(hours=4), hours=1, job_id="two")
    intelligence.observe_usage(
        "x2d",
        observed_at=NOW + timedelta(hours=6),
        ha_estimate_hours=100,
        printer_reported_hours=None,
    )

    usage = intelligence.usage_summary("x2d")
    assert usage["printer_reported_lifetime_hours"] is None
    assert usage["ha_bambulab_estimated_usage_hours"] == 100
    assert usage["locally_observed_print_hours"] == 3.0083
    assert usage["maintenance_effective_lifetime_hours"] == 101.0042
    assert usage["cloud_history_job_count"] == 1
    assert usage["cloud_history_known_interval_hours"] not in {
        usage["locally_observed_print_hours"],
        usage["maintenance_effective_lifetime_hours"],
    }


def test_printer_reported_usage_takes_precedence_and_retains_provenance(
    tmp_path: Path,
) -> None:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    intelligence.observe_usage(
        "x2d",
        observed_at=NOW,
        ha_estimate_hours=90,
        printer_reported_hours=250,
    )
    _finish(local, start=NOW + timedelta(hours=1), hours=1, job_id="later")
    intelligence.observe_usage(
        "x2d",
        observed_at=NOW + timedelta(hours=3),
        ha_estimate_hours=91,
        printer_reported_hours=250,
    )

    usage = intelligence.usage_summary("x2d")
    assert usage["printer_reported_lifetime_hours"] == 250
    assert usage["maintenance_effective_lifetime_hours"] == 251.0042
    assert (
        usage["maintenance_effective_provenance"]
        == "printer_reported_high_water_plus_local_delta"
    )


def test_maintenance_thresholds_and_append_only_completion_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    configured = (
        MaintenanceTaskSettings(
            "user_inspection",
            "User-defined inspection",
            interval_hours=10,
            warning_hours=2,
            interval_prints=2,
            warning_prints=1,
            due_when="any",
            source="user_configured",
        ),
        MaintenanceTaskSettings(
            "job_service",
            "Job-count service",
            interval_prints=1,
            source="user_configured",
        ),
        MaintenanceTaskSettings(
            "calendar_service",
            "Calendar service",
            interval_days=1,
            source="user_configured",
        ),
    )
    intelligence.sync_maintenance_tasks(configured, now=NOW, manufacturer_tasks=())

    # A configured task without local completion history cannot be evaluated
    # yet: the dashboard does not know when the work was last performed.
    pending = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW + timedelta(days=2))[
            "tasks"
        ]
    }
    assert {task["state"] for task in pending.values()} == {"baseline_required"}
    assert all(task["overdue"] is False for task in pending.values())

    intelligence.complete_all_maintenance(
        notes="Baseline", completed_at=NOW, printer_id="x2d"
    )
    _finish(local, start=NOW, hours=9, job_id="one")
    initial = intelligence.maintenance("x2d", now=NOW + timedelta(days=2))
    tasks = {task["maintenance_task_id"]: task for task in initial["tasks"]}
    task = tasks["user_inspection"]
    assert task["warning"] is True and task["due"] is False
    assert task["state"] == "due_soon"
    assert tasks["job_service"]["state"] == "due"
    assert tasks["job_service"]["due"] is True
    assert tasks["job_service"]["overdue"] is False
    assert tasks["calendar_service"]["overdue"] is True
    assert tasks["calendar_service"]["triggers"][0]["next_due_at"].startswith(
        "2026-08-13T12:00:00"
    )

    first = intelligence.complete_maintenance(
        "user_inspection",
        notes="Inspected locally",
        completed_at=NOW + timedelta(hours=10),
        printer_id="x2d",
    )
    second = intelligence.complete_maintenance(
        "user_inspection",
        notes="Later service",
        completed_at=NOW + timedelta(days=1),
        printer_id="x2d",
    )
    result = intelligence.maintenance("x2d", now=NOW + timedelta(days=1))
    assert first["printer_control"] is False and second["printer_control"] is False
    assert len(result["completion_history"]) == 5
    assert result["completion_history"][0]["notes"] == "Later service"
    updated = {task["maintenance_task_id"]: task for task in result["tasks"]}
    assert updated["user_inspection"]["state"] == "ok"

    intelligence.sync_maintenance_tasks(
        (), now=NOW + timedelta(days=2), manufacturer_tasks=()
    )
    disabled = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW + timedelta(days=2))[
            "tasks"
        ]
    }
    assert all(task["enabled"] is False for task in disabled.values())
    assert len(intelligence.maintenance("x2d")["completion_history"]) == 5


def test_printer_monitoring_is_restart_safe_and_coexists_with_manual_session(
    tmp_path: Path,
) -> None:
    monitoring = MonitoringExportStore(
        tmp_path / "monitoring.sqlite3", tmp_path / "exports"
    )
    monitoring.initialize()
    manual_request = MonitoringRequest(
        "Manual interval",
        "",
        3600,
        (Source("air_quality", location="office"),),
        ("pm25", "voc_index"),
        "raw",
        "wide",
    )
    monitoring.create_session(
        manual_request,
        start_time=NOW,
        scheduled_end_time=NOW + timedelta(hours=1),
    )
    printer = PrinterStore(tmp_path / "printer.sqlite3")
    printer.initialize()
    active = printer.process(_state(NOW, NormalizedPrinterState.PREPARING))[0]
    coordinator = PrinterMonitoringCoordinator(
        monitoring,
        environment_location="office",
        recovery_minutes=120,
        sensor_status_provider=lambda _location, _now: {
            "status": "online",
            "last_seen": "2026-08-12T12:00:00Z",
        },
    )

    first = coordinator.synchronize(active)
    second = coordinator.synchronize(active)
    sessions = monitoring.list_sessions()
    assert first["id"] == second["id"]
    assert len(sessions) == 2
    assert {item["trigger_source"] for item in sessions} == {"manual", "printer"}
    printer_monitoring = next(
        item for item in sessions if item["trigger_source"] == "printer"
    )
    assert printer_monitoring["printer_session_id"] == active.session_id
    restarted_store = MonitoringExportStore(
        tmp_path / "monitoring.sqlite3", tmp_path / "exports"
    )
    restarted_store.initialize()
    restarted = PrinterMonitoringCoordinator(
        restarted_store,
        environment_location="office",
        recovery_minutes=120,
        sensor_status_provider=lambda _location, _now: {
            "status": "online",
            "last_seen": "2026-08-12T12:00:00Z",
        },
    ).synchronize(active)
    assert restarted["id"] == printer_monitoring["id"]


def test_post_print_monitoring_closes_only_after_recovery(tmp_path: Path) -> None:
    monitoring = MonitoringExportStore(
        tmp_path / "monitoring.sqlite3", tmp_path / "exports"
    )
    monitoring.initialize()
    printer = PrinterStore(tmp_path / "printer.sqlite3")
    printer.initialize()
    active = printer.process(_state(NOW, NormalizedPrinterState.PRINTING))[0]
    coordinator = PrinterMonitoringCoordinator(
        monitoring,
        environment_location="office",
        recovery_minutes=120,
        sensor_status_provider=lambda _location, _now: {
            "status": "online",
            "last_seen": "2026-08-12T12:00:00Z",
        },
    )
    coordinator.synchronize(active)
    printer.process(_state(NOW + timedelta(hours=1), NormalizedPrinterState.COMPLETED))
    closed = printer.process(
        _state(NOW + timedelta(hours=1, seconds=15), NormalizedPrinterState.COMPLETED)
    )[0]
    scheduled = coordinator.synchronize(closed)
    assert scheduled["status"] == "running"
    assert scheduled["printer_ended_at_utc"].startswith("2026-08-12T13:00:15")
    assert monitoring.reconcile_due_sessions(NOW + timedelta(hours=2)) == 0
    assert monitoring.reconcile_due_sessions(NOW + timedelta(hours=4)) == 1
    completed = monitoring.get_session(scheduled["id"])
    assert completed["status"] == "completed"
    assert monitoring.get_export_for_session(scheduled["id"]) is not None


def test_offline_sen66_skips_print_monitoring_without_creating_empty_session(
    tmp_path: Path,
) -> None:
    monitoring = MonitoringExportStore(
        tmp_path / "monitoring.sqlite3", tmp_path / "exports"
    )
    monitoring.initialize()
    printer = PrinterStore(tmp_path / "printer.sqlite3")
    printer.initialize()
    active = printer.process(_state(NOW, NormalizedPrinterState.PRINTING))[0]
    status = {"status": "offline", "last_seen": "2026-08-11T12:00:00Z"}
    coordinator = PrinterMonitoringCoordinator(
        monitoring,
        environment_location="office",
        recovery_minutes=120,
        sensor_status_provider=lambda _location, _now: status,
    )

    skipped = coordinator.synchronize(active, observed_at=NOW)
    status["status"] = "online"
    repeated = coordinator.synchronize(active, observed_at=NOW + timedelta(minutes=1))

    assert skipped["state"] == "skipped"
    assert "offline" in skipped["reason"]
    assert repeated["state"] == "skipped"
    assert monitoring.list_sessions() == []


def test_running_print_monitoring_degrades_and_recovers_without_a_second_session(
    tmp_path: Path,
) -> None:
    monitoring = MonitoringExportStore(
        tmp_path / "monitoring.sqlite3", tmp_path / "exports"
    )
    monitoring.initialize()
    printer = PrinterStore(tmp_path / "printer.sqlite3")
    printer.initialize()
    active = printer.process(_state(NOW, NormalizedPrinterState.PRINTING))[0]
    status = {"status": "online", "last_seen": "2026-08-12T12:00:00Z"}
    coordinator = PrinterMonitoringCoordinator(
        monitoring,
        environment_location="office",
        recovery_minutes=120,
        sensor_status_provider=lambda _location, _now: status,
    )

    started = coordinator.synchronize(active, observed_at=NOW)
    status["status"] = "offline"
    degraded = coordinator.synchronize(active, observed_at=NOW + timedelta(minutes=1))
    status["status"] = "online"
    recovered = coordinator.synchronize(active, observed_at=NOW + timedelta(minutes=2))

    assert started["sensor_monitoring"]["state"] == "running"
    assert degraded["sensor_monitoring"]["state"] == "degraded"
    assert recovered["sensor_monitoring"]["state"] == "running"
    assert len(monitoring.list_sessions()) == 1
    assert {started["id"], degraded["id"], recovered["id"]} == {started["id"]}
