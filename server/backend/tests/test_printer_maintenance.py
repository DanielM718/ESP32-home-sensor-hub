"""Tracked print time, manufacturer maintenance, and notification tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.config import ConfigError
from app.printer_config import MaintenanceTaskSettings, PrinterObserverSettings
from app.printer_intelligence import PrinterIntelligenceError, PrinterIntelligenceStore
from app.printer_maintenance import (
    MANUFACTURER_SOURCE,
    MANUFACTURER_SOURCE_URL,
    MODE_HEAVY_USE,
    MODE_LOW_USE,
    MODE_NORMAL,
    X2D_MAINTENANCE_TASKS,
    LoggingMaintenanceNotifier,
    add_months,
    maintenance_mode,
)
from app.printer_persistence import PrinterStore

NOW = datetime(2026, 8, 15, 12, tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _store(tmp_path: Path, **kwargs) -> PrinterIntelligenceStore:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    intelligence = PrinterIntelligenceStore(database, **kwargs)
    intelligence.initialize()
    return intelligence


def _session(
    intelligence: PrinterIntelligenceStore,
    session_id: str,
    *,
    started: str | None,
    ended: str | None,
    result: str | None = "completed",
    printer_id: str = "x2d",
    source: str = "locally_observed",
    job_id: str | None = None,
    job_name: str | None = None,
) -> None:
    connection = sqlite3.connect(intelligence.database_path)
    with connection:
        connection.execute(
            """INSERT INTO print_sessions (
                   session_id, printer_id, job_id, job_name, started_at_utc,
                   start_provenance, ended_at_utc, end_provenance, result,
                   material, material_provenance, active_tool, ams_slot, source,
                   updated_at_utc
               ) VALUES (?, ?, ?, ?, ?, 'observed', ?, 'observed', ?, NULL,
                         'observed', NULL, NULL, ?, ?)""",
            (
                session_id,
                printer_id,
                job_id,
                job_name,
                started if started is not None else "",
                ended,
                result,
                source,
                _iso(NOW),
            ),
        )
    connection.close()


def _cloud_record(
    cloud_id: str, *, start: str | None, end: str | None, status: int = 2
):
    return {
        "id": cloud_id,
        "title": f"job-{cloud_id}",
        "status": status,
        "startTime": start,
        "endTime": end,
        "costTime": 999_999,
        "amsDetailMapping": [],
    }


def _print(
    intelligence: PrinterIntelligenceStore,
    index: int,
    *,
    start: datetime,
    hours: float,
    result: str = "completed",
) -> None:
    _session(
        intelligence,
        f"local-{index}",
        started=_iso(start),
        ended=_iso(start + timedelta(hours=hours)),
        result=result,
    )


# --------------------------------------------------------------------------
# Canonical tracked print time
# --------------------------------------------------------------------------


def test_tracked_runtime_counts_every_known_interval_regardless_of_outcome(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    _session(
        intelligence,
        "completed",
        started=_iso(NOW - timedelta(hours=10)),
        ended=_iso(NOW - timedelta(hours=8)),
        result="completed",
    )
    _session(
        intelligence,
        "failed",
        started=_iso(NOW - timedelta(hours=7)),
        ended=_iso(NOW - timedelta(hours=6)),
        result="failed",
    )
    _session(
        intelligence,
        "cancelled",
        started=_iso(NOW - timedelta(hours=5)),
        ended=_iso(NOW - timedelta(hours=4, minutes=30)),
        result="cancelled",
    )
    _session(
        intelligence,
        "unknown-result",
        started=_iso(NOW - timedelta(hours=3)),
        ended=_iso(NOW - timedelta(hours=2)),
        result="unknown",
    )

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["tracked_print_seconds"] == int(4.5 * 3600)
    assert tracked["tracked_print_hours"] == 4.5
    assert tracked["tracked_job_count"] == 4
    assert tracked["tracked_completed_count"] == 1
    assert tracked["tracked_failed_or_cancelled_count"] == 2
    assert tracked["tracked_unknown_result_count"] == 1
    assert tracked["tracked_first_print_at"] == _iso(NOW - timedelta(hours=10))
    assert tracked["tracked_last_print_at"] == _iso(NOW - timedelta(hours=2))
    assert tracked["tracked_history_provenance"] == ["locally_observed"]


@pytest.mark.parametrize(
    ("started", "ended"),
    [
        (None, _iso(NOW)),
        (_iso(NOW - timedelta(hours=1)), None),
        (_iso(NOW), _iso(NOW - timedelta(hours=1))),
        ("not-a-timestamp", _iso(NOW)),
    ],
)
def test_tracked_runtime_never_invents_a_missing_or_invalid_interval(
    tmp_path: Path, started: str | None, ended: str | None
) -> None:
    intelligence = _store(tmp_path)
    _session(intelligence, "broken", started=started, ended=ended)

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["tracked_print_seconds"] == 0
    assert tracked["tracked_job_count"] == 0
    assert tracked["tracked_unknown_interval_job_count"] == 1
    assert tracked["tracked_first_print_at"] is None
    assert tracked["tracked_history_complete"] is False


def test_tracked_runtime_uses_cloud_only_history_and_ignores_slicer_estimates(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.import_cloud_records(
        "x2d",
        [
            _cloud_record(
                "1",
                start=_iso(NOW - timedelta(hours=6)),
                end=_iso(NOW - timedelta(hours=4)),
            ),
            _cloud_record(
                "2",
                start=_iso(NOW - timedelta(hours=3)),
                end=_iso(NOW - timedelta(hours=2)),
                status=3,
            ),
            _cloud_record("3", start=None, end=None),
        ],
        imported_at=NOW,
    )

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    # costTime is 999999 seconds on every record; it must never be used.
    assert tracked["tracked_print_seconds"] == 3 * 3600
    assert tracked["tracked_job_count"] == 2
    assert tracked["tracked_completed_count"] == 1
    assert tracked["tracked_failed_or_cancelled_count"] == 1
    assert tracked["tracked_unknown_interval_job_count"] == 1
    assert tracked["tracked_history_provenance"] == ["bambu_cloud_history"]


def test_reconciled_local_and_cloud_history_is_counted_exactly_once(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    start = NOW - timedelta(hours=5)
    end = NOW - timedelta(hours=3)
    _session(
        intelligence,
        "local-1",
        started=_iso(start),
        ended=_iso(end),
        job_id="900",
        job_name="job-900",
    )
    intelligence.import_cloud_records(
        "x2d", [_cloud_record("900", start=_iso(start), end=_iso(end))], imported_at=NOW
    )

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["tracked_job_count"] == 1
    assert tracked["tracked_print_seconds"] == 2 * 3600
    assert tracked["tracked_history_provenance"] == ["locally_observed"]


def test_reconciled_cloud_interval_covers_a_local_session_without_an_end(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    start = NOW - timedelta(hours=5)
    end = NOW - timedelta(hours=3)
    _session(
        intelligence,
        "local-1",
        started=_iso(start),
        ended=None,
        result=None,
        job_id="900",
        job_name="job-900",
    )
    intelligence.import_cloud_records(
        "x2d", [_cloud_record("900", start=_iso(start), end=_iso(end))], imported_at=NOW
    )

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["tracked_job_count"] == 1
    assert tracked["tracked_print_seconds"] == 2 * 3600
    assert tracked["tracked_history_provenance"] == ["bambu_cloud_history_reconciled"]


def test_tracked_runtime_is_exposed_through_usage_summary_beside_legacy_fields(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    _print(intelligence, 1, start=NOW - timedelta(hours=4), hours=2)

    usage = intelligence.usage_summary("x2d", now=NOW)

    assert usage["tracked_print_hours"] == 2.0
    # Pre-existing contract fields remain available for Butters.
    for legacy in (
        "locally_observed_print_hours",
        "locally_observed_completed_print_count",
        "maintenance_effective_lifetime_hours",
        "maintenance_effective_provenance",
        "printer_reported_lifetime_hours",
        "ha_bambulab_estimated_usage_hours",
        "cloud_history_known_interval_hours",
        "cloud_history_job_count",
    ):
        assert legacy in usage


def test_rolling_utilization_clips_intervals_to_the_window(tmp_path: Path) -> None:
    intelligence = _store(tmp_path, rolling_window_days=30)
    # One long print that starts before the window and ends inside it.
    _session(
        intelligence,
        "straddle",
        started=_iso(NOW - timedelta(days=31)),
        ended=_iso(NOW - timedelta(days=29)),
    )

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["tracked_print_hours"] == 48.0
    assert tracked["rolling_tracked_print_hours"] == 24.0
    assert tracked["rolling_tracked_history_days"] == 30.0


# --------------------------------------------------------------------------
# Manufacturer usage tiers (heavy-use mode)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours_per_day", "expected"),
    [
        (5.0, MODE_HEAVY_USE),
        (5.1, MODE_HEAVY_USE),
        (4.99, MODE_NORMAL),
        (1.0, MODE_NORMAL),
        (0.99, MODE_LOW_USE),
        (0.0, MODE_LOW_USE),
    ],
)
def test_manufacturer_usage_tier_boundaries(
    hours_per_day: float, expected: str
) -> None:
    mode, reason = maintenance_mode(hours_per_day, history_days=30)
    assert mode == expected
    assert reason.startswith("tracked_average")


def test_insufficient_history_falls_back_to_the_normal_tier(tmp_path: Path) -> None:
    intelligence = _store(tmp_path, minimum_mode_history_days=7)
    _print(intelligence, 1, start=NOW - timedelta(days=2), hours=20)

    tracked = intelligence.tracked_runtime("x2d", now=NOW)

    assert tracked["rolling_tracked_print_hours_per_day"] > 5
    assert tracked["maintenance_mode"] == MODE_NORMAL
    assert tracked["maintenance_mode_reason"] == "insufficient_tracked_history"


def test_no_history_at_all_reports_the_normal_tier_without_guessing() -> None:
    assert maintenance_mode(None, history_days=None) == (
        MODE_NORMAL,
        "insufficient_tracked_history",
    )


def test_heavy_use_shortens_only_the_manufacturer_tiered_intervals(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=60))
    intelligence.complete_all_maintenance(
        notes="baseline", completed_at=NOW - timedelta(days=60), printer_id="x2d"
    )
    for index in range(20):
        _print(intelligence, index, start=NOW - timedelta(days=20 - index), hours=6)

    result = intelligence.maintenance("x2d", now=NOW)
    tasks = {task["maintenance_task_id"]: task for task in result["tasks"]}

    assert result["usage"]["maintenance_mode"] == MODE_HEAVY_USE
    assert tasks["x2d_xy_axis_clean_lubricate"]["applied_interval_months"] == 1
    assert tasks["x2d_z_axis_deep_maintenance"]["applied_interval_months"] == 3
    # A fixed manufacturer cadence is never rescaled by usage.
    assert tasks["x2d_live_view_camera_cleaning"]["applied_interval_months"] == 6
    assert tasks["x2d_xy_axis_clean_lubricate"]["state"] == "overdue"
    assert tasks["x2d_z_axis_deep_maintenance"]["state"] == "ok"


def test_material_history_is_not_used_as_an_automated_maintenance_trigger() -> None:
    # Bambu Lab shortens camera cleaning for volatile materials and scales the
    # cutter check by filament rolls, but publishes no automatable number.
    camera = next(
        task
        for task in X2D_MAINTENANCE_TASKS
        if task.task_id == "x2d_live_view_camera_cleaning"
    )
    cutter = next(
        task
        for task in X2D_MAINTENANCE_TASKS
        if task.task_id == "x2d_filament_cutter_blade"
    )
    assert "ABS" in camera.notes
    assert camera.interval_months == 6
    assert cutter.trigger_kind == "manual_inspection"
    assert "rolls" in cutter.cadence


# --------------------------------------------------------------------------
# Manufacturer catalog and trigger semantics
# --------------------------------------------------------------------------


def test_manufacturer_catalog_is_seeded_with_source_provenance(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW)

    result = intelligence.maintenance("x2d", now=NOW)
    tasks = {task["maintenance_task_id"]: task for task in result["tasks"]}

    assert len(tasks) == len(X2D_MAINTENANCE_TASKS)
    for task in tasks.values():
        assert task["manufacturer_source"] == MANUFACTURER_SOURCE
        assert task["manufacturer_source_url"] == MANUFACTURER_SOURCE_URL
        assert task["cadence"]
        assert task["local_record_only"] is True
        assert task["printer_control"] is False
    assert result["manufacturer_source"]["url"] == MANUFACTURER_SOURCE_URL
    assert (
        tasks["x2d_xy_axis_clean_lubricate"]["triggers"][0]["trigger_type"]
        == "usage_tiered_calendar_months"
    )


def test_configuration_can_override_a_manufacturer_task(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks(
        (
            MaintenanceTaskSettings(
                "x2d_live_view_camera_cleaning",
                "Camera cleaning (operator schedule)",
                interval_days=45,
                source="user_configured",
            ),
        ),
        now=NOW,
    )

    tasks = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    overridden = tasks["x2d_live_view_camera_cleaning"]

    assert overridden["provenance"] == "user_configured"
    assert overridden["trigger_kind"] == "threshold"
    assert len(tasks) == len(X2D_MAINTENANCE_TASKS)


def test_calendar_month_cadence_uses_real_months_not_thirty_day_blocks() -> None:
    assert add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1) == datetime(
        2026, 2, 28, tzinfo=timezone.utc
    )
    assert add_months(datetime(2026, 12, 15, tzinfo=timezone.utc), 6) == datetime(
        2027, 6, 15, tzinfo=timezone.utc
    )


def test_event_driven_calibration_becomes_due_only_after_axis_service(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=200))
    calibration = "x2d_full_calibration_after_axis_service"

    idle = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    assert idle[calibration]["state"] == "ok"
    assert idle[calibration]["trigger_kind"] == "event_after_task"

    intelligence.complete_maintenance(
        "x2d_xy_axis_clean_lubricate",
        notes="axis service",
        completed_at=NOW - timedelta(hours=1),
        printer_id="x2d",
    )
    after_service = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    assert after_service[calibration]["state"] == "due"
    assert after_service[calibration]["due"] is True

    intelligence.complete_maintenance(
        calibration, notes="calibrated", completed_at=NOW, printer_id="x2d"
    )
    after_calibration = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    assert after_calibration[calibration]["state"] == "ok"


def test_condition_based_tasks_stay_advisory_and_never_become_overdue(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=3650))

    tasks = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    advisory = [task for task in tasks.values() if task["state"] == "advisory"]

    assert {task["maintenance_task_id"] for task in advisory} == {
        "x2d_chamber_interior_cleaning",
        "x2d_build_plate_cleaning",
        "x2d_main_extruder_cleaning",
        "x2d_activated_carbon_filter",
        "x2d_silicone_nozzle_wiper",
        "x2d_auxiliary_ptfe_tube",
        "x2d_filament_cutter_blade",
    }
    for task in advisory:
        assert task["due"] is False and task["overdue"] is False
        assert task["baseline_required"] is False
        assert (
            "no numeric interval published" in task["cadence"]
            or "rolls" in task["cadence"]
        )


# --------------------------------------------------------------------------
# Baseline, lifecycle states, and the local completion audit
# --------------------------------------------------------------------------


def test_new_tasks_require_a_baseline_before_any_schedule_applies(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=400))

    result = intelligence.maintenance("x2d", now=NOW)
    scheduled = [
        task
        for task in result["tasks"]
        if task["trigger_kind"]
        in {"calendar_months", "usage_tiered_calendar_months", "threshold"}
    ]

    assert scheduled
    for task in scheduled:
        assert task["state"] == "baseline_required"
        assert task["baseline_required"] is True
        assert task["overdue"] is False and task["due"] is False
        assert task["next_due_at"] is None
    assert result["summary"]["baseline_required_count"] == len(scheduled)


def test_mark_all_completed_establishes_the_baseline_for_every_enabled_task(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=400))

    result = intelligence.complete_all_maintenance(
        notes="installed today", completed_at=NOW, printer_id="x2d"
    )
    after = intelligence.maintenance("x2d", now=NOW)

    assert result["completed_task_count"] == len(X2D_MAINTENANCE_TASKS)
    assert result["local_record_only"] is True and result["printer_control"] is False
    assert after["summary"]["baseline_required_count"] == 0
    assert after["summary"]["overall_state"] in {"ok", "advisory"}
    assert len(after["completion_history"]) == len(X2D_MAINTENANCE_TASKS)


def test_completion_history_is_append_only_and_keeps_every_prior_record(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW - timedelta(days=400))
    for offset in (30, 20, 10):
        intelligence.complete_maintenance(
            "x2d_live_view_camera_cleaning",
            notes=f"service {offset}",
            completed_at=NOW - timedelta(days=offset),
            printer_id="x2d",
        )

    history = intelligence.maintenance("x2d", now=NOW)["completion_history"]
    camera = [
        item
        for item in history
        if item["maintenance_task_id"] == "x2d_live_view_camera_cleaning"
    ]

    assert [item["notes"] for item in camera] == [
        "service 10",
        "service 20",
        "service 30",
    ]
    assert len({item["event_id"] for item in camera}) == 3


def test_due_soon_due_and_overdue_progression_then_return_to_ok(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW)
    baseline = NOW
    intelligence.complete_all_maintenance(
        notes="baseline", completed_at=baseline, printer_id="x2d"
    )
    camera = "x2d_live_view_camera_cleaning"

    def state_at(moment: datetime) -> str:
        tasks = {
            task["maintenance_task_id"]: task
            for task in intelligence.maintenance("x2d", now=moment)["tasks"]
        }
        return tasks[camera]["state"]

    due_at = add_months(baseline, 6)
    assert state_at(baseline + timedelta(days=1)) == "ok"
    assert state_at(due_at - timedelta(days=3)) == "due_soon"
    assert state_at(due_at) == "due"
    assert state_at(due_at + timedelta(days=5)) == "overdue"

    intelligence.complete_maintenance(
        camera,
        notes="cleaned",
        completed_at=due_at + timedelta(days=5),
        printer_id="x2d",
    )
    assert state_at(due_at + timedelta(days=6)) == "ok"


def test_local_warning_lead_time_is_not_attributed_to_the_manufacturer(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW)
    intelligence.complete_all_maintenance(
        notes="baseline", completed_at=NOW, printer_id="x2d"
    )

    tasks = {
        task["maintenance_task_id"]: task
        for task in intelligence.maintenance("x2d", now=NOW)["tasks"]
    }
    camera = tasks["x2d_live_view_camera_cleaning"]

    assert camera["manufacturer_source"] == MANUFACTURER_SOURCE
    assert camera["warning_source"] == "local_dashboard_policy"
    assert camera["triggers"][0]["warning_threshold"] == 14
    assert camera["triggers"][0]["warning_threshold_unit"] == "days"
    assert camera["triggers"][0]["interval_unit"] == "calendar_months"


def test_completing_an_unknown_or_disabled_task_is_rejected(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW, manufacturer_tasks=())

    with pytest.raises(PrinterIntelligenceError):
        intelligence.complete_maintenance(
            "x2d_live_view_camera_cleaning", notes="", completed_at=NOW
        )
    with pytest.raises(PrinterIntelligenceError):
        intelligence.complete_all_maintenance(notes="", completed_at=NOW)
    with pytest.raises(PrinterIntelligenceError):
        intelligence.complete_maintenance("Invalid Id", notes="", completed_at=NOW)


# --------------------------------------------------------------------------
# Durable, edge-triggered notifications
# --------------------------------------------------------------------------


def _catalog(*task_ids: str):
    return tuple(task for task in X2D_MAINTENANCE_TASKS if task.task_id in task_ids)


def _baselined(
    tmp_path: Path, *task_ids: str
) -> tuple[PrinterIntelligenceStore, datetime]:
    """Seed a narrow catalog so one subject's lifecycle is unambiguous."""

    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks(
        (),
        now=NOW,
        manufacturer_tasks=_catalog(*(task_ids or ("x2d_live_view_camera_cleaning",))),
    )
    baseline = NOW
    intelligence.complete_all_maintenance(
        notes="baseline", completed_at=baseline, printer_id="x2d"
    )
    return intelligence, baseline


def test_each_transition_emits_exactly_one_event_and_polling_does_not_repeat_it(
    tmp_path: Path,
) -> None:
    intelligence, baseline = _baselined(tmp_path)
    due_at = add_months(baseline, 6)
    camera = "x2d_live_view_camera_cleaning"

    def emitted(moment: datetime) -> list[tuple[str, str]]:
        return [
            (event["event_type"], event["subject_id"])
            for event in intelligence.evaluate_maintenance_events("x2d", now=moment)
        ]

    assert emitted(baseline + timedelta(days=1)) == []
    first = emitted(due_at - timedelta(days=3))
    assert first == [("maintenance_due_soon", camera)]
    # Repeated observer polls in the same state append nothing.
    assert emitted(due_at - timedelta(days=2)) == []
    assert emitted(due_at - timedelta(days=1)) == []
    assert emitted(due_at) == [("maintenance_due", camera)]
    assert emitted(due_at + timedelta(days=1)) == [("maintenance_overdue", camera)]
    assert emitted(due_at + timedelta(days=2)) == []


def test_a_restart_does_not_resend_an_unchanged_state(tmp_path: Path) -> None:
    intelligence, baseline = _baselined(tmp_path)
    overdue_at = add_months(baseline, 6) + timedelta(days=1)
    assert intelligence.evaluate_maintenance_events("x2d", now=overdue_at)

    restarted = PrinterIntelligenceStore(intelligence.database_path)
    restarted.initialize()

    assert (
        restarted.evaluate_maintenance_events(
            "x2d", now=overdue_at + timedelta(hours=1)
        )
        == []
    )


def test_tasks_deduplicate_independently_of_one_another(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks(
        (),
        now=NOW,
        manufacturer_tasks=_catalog(
            "x2d_live_view_camera_cleaning", "x2d_xy_axis_clean_lubricate"
        ),
    )
    intelligence.complete_maintenance(
        "x2d_live_view_camera_cleaning",
        notes="baseline",
        completed_at=NOW - timedelta(days=400),
        printer_id="x2d",
    )
    intelligence.complete_maintenance(
        "x2d_xy_axis_clean_lubricate",
        notes="baseline",
        completed_at=NOW,
        printer_id="x2d",
    )

    subjects = {
        event["subject_id"]: event["event_type"]
        for event in intelligence.notification_events()
    }

    # The stale camera task alerts; the freshly serviced axis task stays quiet.
    assert subjects["x2d_live_view_camera_cleaning"] == "maintenance_overdue"
    assert "x2d_xy_axis_clean_lubricate" not in subjects
    assert intelligence.evaluate_maintenance_events("x2d", now=NOW) == []


def test_completion_resets_the_notification_lifecycle(tmp_path: Path) -> None:
    intelligence, baseline = _baselined(tmp_path)
    overdue_at = add_months(baseline, 6) + timedelta(days=2)
    intelligence.evaluate_maintenance_events("x2d", now=overdue_at)

    result = intelligence.complete_maintenance(
        "x2d_live_view_camera_cleaning",
        notes="cleaned",
        completed_at=overdue_at,
        printer_id="x2d",
    )

    assert [event["event_type"] for event in result["notification_events"]] == [
        "maintenance_returned_to_ok"
    ]
    assert intelligence.evaluate_maintenance_events("x2d", now=overdue_at) == []


def test_returning_to_ok_is_not_announced_without_a_prior_problem_state(
    tmp_path: Path,
) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW)
    intelligence.evaluate_maintenance_events("x2d", now=NOW)

    events = intelligence.complete_all_maintenance(
        notes="baseline", completed_at=NOW, printer_id="x2d"
    )["notification_events"]

    assert [event["event_type"] for event in events] == []


def test_heavy_use_mode_entry_and_exit_are_edge_triggered(tmp_path: Path) -> None:
    intelligence = _store(tmp_path)
    intelligence.sync_maintenance_tasks((), now=NOW, manufacturer_tasks=())
    for index in range(20):
        _print(intelligence, index, start=NOW - timedelta(days=20 - index), hours=6)

    entered = intelligence.evaluate_maintenance_events("x2d", now=NOW)
    assert [event["event_type"] for event in entered] == ["heavy_use_mode_entered"]
    assert intelligence.evaluate_maintenance_events("x2d", now=NOW) == []

    # Thirty quiet days later the rolling average leaves the heavy-use tier.
    later = NOW + timedelta(days=31)
    exited = intelligence.evaluate_maintenance_events("x2d", now=later)
    assert [event["event_type"] for event in exited] == ["heavy_use_mode_exited"]
    assert intelligence.evaluate_maintenance_events("x2d", now=later) == []


def test_pending_events_are_dispatched_once_and_recorded_as_delivered(
    tmp_path: Path,
) -> None:
    intelligence, baseline = _baselined(tmp_path)
    intelligence.evaluate_maintenance_events("x2d", now=add_months(baseline, 6))
    delivered: list[str] = []

    class _Notifier:
        def deliver(self, event) -> str:
            delivered.append(str(event["event_type"]))
            return "delivered"

    first = intelligence.dispatch_notifications(_Notifier(), now=baseline)
    second = intelligence.dispatch_notifications(_Notifier(), now=baseline)

    assert first == 1 and second == 0
    assert delivered == ["maintenance_due"]
    assert intelligence.notification_events(pending_only=True) == []
    stored = intelligence.notification_events()[0]
    assert stored["delivery_status"] == "delivered"
    assert stored["delivered_at"] is not None


def test_a_failing_notifier_keeps_the_event_pending(tmp_path: Path) -> None:
    intelligence, baseline = _baselined(tmp_path)
    intelligence.evaluate_maintenance_events("x2d", now=add_months(baseline, 6))

    class _Broken:
        def deliver(self, event) -> str:
            raise RuntimeError("transport down")

    assert intelligence.dispatch_notifications(_Broken(), now=baseline) == 0
    assert len(intelligence.notification_events(pending_only=True)) == 1


def test_default_notifier_records_events_without_external_credentials(
    tmp_path: Path,
) -> None:
    intelligence, baseline = _baselined(tmp_path)
    intelligence.evaluate_maintenance_events("x2d", now=add_months(baseline, 6))

    assert intelligence.dispatch_notifications(LoggingMaintenanceNotifier()) == 1
    assert intelligence.notification_events()[0]["delivery_status"] == "logged"


# --------------------------------------------------------------------------
# Schema migration safety
# --------------------------------------------------------------------------


def test_migration_is_idempotent_and_preserves_prior_maintenance_records(
    tmp_path: Path,
) -> None:
    database = tmp_path / "printer.sqlite3"
    legacy = sqlite3.connect(database)
    with legacy:
        legacy.executescript(
            """
            CREATE TABLE maintenance_tasks (
                task_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '', interval_hours REAL,
                warning_hours REAL NOT NULL DEFAULT 0, interval_prints INTEGER,
                warning_prints INTEGER NOT NULL DEFAULT 0, interval_days INTEGER,
                warning_days INTEGER NOT NULL DEFAULT 0,
                due_when TEXT NOT NULL CHECK (due_when IN ('any', 'all')),
                notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1, created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            CREATE TABLE maintenance_completion_events (
                event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                completed_at_utc TEXT NOT NULL, effective_usage_hours REAL NOT NULL,
                completed_print_count INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                recorded_by TEXT NOT NULL DEFAULT 'dashboard_user',
                FOREIGN KEY(task_id) REFERENCES maintenance_tasks(task_id)
            );
            INSERT INTO maintenance_tasks VALUES (
                'legacy_task', 'Legacy inspection', '', 100, 10, NULL, 0, NULL, 0,
                'any', '', 'user_configured', 1, '2026-01-01T00:00:00Z',
                '2026-01-01T00:00:00Z'
            );
            INSERT INTO maintenance_completion_events VALUES (
                'legacy-event', 'legacy_task', '2026-07-01T00:00:00Z', 5.0, 2,
                'historic', 'dashboard_user'
            );
            """
        )
    legacy.close()
    PrinterStore(database).initialize()

    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    intelligence.initialize()  # restart-safe

    result = intelligence.maintenance("x2d", now=NOW)
    tasks = {task["maintenance_task_id"]: task for task in result["tasks"]}

    assert tasks["legacy_task"]["trigger_kind"] == "threshold"
    assert tasks["legacy_task"]["provenance"] == "user_configured"
    assert any(
        item["event_id"] == "legacy-event" for item in result["completion_history"]
    )
    assert tasks["legacy_task"]["last_completed_at"] == "2026-07-01T00:00:00Z"


def test_maintenance_engine_settings_are_validated() -> None:
    base = {
        "printer_id": "x2d",
        "printer_model": "X2D",
        "home_assistant_url": "http://127.0.0.1:8123",
        "entities": {
            "online": "binary_sensor.x_online",
            "print_status": "sensor.x_print_status",
        },
    }
    assert (
        PrinterObserverSettings(**base).validated().maintenance_rolling_window_days
        == 30
    )
    with pytest.raises(ConfigError):
        PrinterObserverSettings(**base, maintenance_evaluation_seconds=5).validated()
    with pytest.raises(ConfigError):
        PrinterObserverSettings(**base, maintenance_rolling_window_days=0).validated()
    with pytest.raises(ConfigError):
        PrinterObserverSettings(
            **base,
            maintenance_rolling_window_days=5,
            maintenance_minimum_history_days=10,
        ).validated()
