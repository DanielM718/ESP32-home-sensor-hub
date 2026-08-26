"""Manufacturer-sourced X2D maintenance catalog, trigger semantics, and events.

Every numeric cadence in this module is quoted from current first-party Bambu
Lab documentation. Guidance that the manufacturer expresses as a condition
("when heavily contaminated", "regularly", "if damaged") is represented as a
non-scheduled advisory task instead of being converted into an invented
numeric interval.
"""

from __future__ import annotations

import calendar
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

LOGGER = logging.getLogger("home_sensor.printer.maintenance")

MANUFACTURER_SOURCE = "bambu_lab_x2d_wiki_periodic_maintenance"
MANUFACTURER_SOURCE_URL = (
    "https://wiki.bambulab.com/en/x2d/maintenance/periodic-maintenance"
)
# Wiki page "updated-at" attribute observed when the catalog was authored.
MANUFACTURER_SOURCE_REVISION = "2026-04-22"
MANUFACTURER_SOURCE_RETRIEVED = "2026-08-15"

LOCAL_POLICY_SOURCE = "local_dashboard_policy"

# Trigger kinds. `threshold` preserves the pre-existing generic engine
# (operating hours / completed prints / calendar days) used by user tasks.
TRIGGER_THRESHOLD = "threshold"
TRIGGER_CALENDAR_MONTHS = "calendar_months"
TRIGGER_USAGE_TIERED_MONTHS = "usage_tiered_calendar_months"
TRIGGER_EVENT_AFTER_TASK = "event_after_task"
TRIGGER_MANUAL_INSPECTION = "manual_inspection"

TRIGGER_KINDS = frozenset(
    {
        TRIGGER_THRESHOLD,
        TRIGGER_CALENDAR_MONTHS,
        TRIGGER_USAGE_TIERED_MONTHS,
        TRIGGER_EVENT_AFTER_TASK,
        TRIGGER_MANUAL_INSPECTION,
    }
)

# Tasks that accumulate toward a threshold cannot be evaluated until the
# operator states when the work was last physically performed.
BASELINE_TRIGGER_KINDS = frozenset(
    {TRIGGER_THRESHOLD, TRIGGER_CALENDAR_MONTHS, TRIGGER_USAGE_TIERED_MONTHS}
)

STATE_BASELINE_REQUIRED = "baseline_required"
STATE_ADVISORY = "advisory"
STATE_OK = "ok"
STATE_DUE_SOON = "due_soon"
STATE_DUE = "due"
STATE_OVERDUE = "overdue"

STATE_SEVERITY = {
    STATE_ADVISORY: 0,
    STATE_OK: 1,
    STATE_BASELINE_REQUIRED: 2,
    STATE_DUE_SOON: 3,
    STATE_DUE: 4,
    STATE_OVERDUE: 5,
}

# Manufacturer usage tiers, quoted from the X2D periodic maintenance page:
# high-frequency >= 5 h/day, regular 1-5 h/day, low-frequency < 1 h/day.
MODE_HEAVY_USE = "heavy_use"
MODE_NORMAL = "normal"
MODE_LOW_USE = "low_use"

HEAVY_USE_HOURS_PER_DAY = 5.0
LOW_USE_HOURS_PER_DAY = 1.0

# Local policy: the manufacturer states an average daily printing time but does
# not define the averaging window or a minimum history length.
DEFAULT_ROLLING_WINDOW_DAYS = 30
MINIMUM_MODE_HISTORY_DAYS = 7

EVENT_DUE_SOON = "maintenance_due_soon"
EVENT_DUE = "maintenance_due"
EVENT_OVERDUE = "maintenance_overdue"
EVENT_RETURNED_TO_OK = "maintenance_returned_to_ok"
EVENT_HEAVY_USE_ENTERED = "heavy_use_mode_entered"
EVENT_HEAVY_USE_EXITED = "heavy_use_mode_exited"

TASK_STATE_EVENTS = {
    STATE_DUE_SOON: EVENT_DUE_SOON,
    STATE_DUE: EVENT_DUE,
    STATE_OVERDUE: EVENT_OVERDUE,
}
PROBLEM_STATES = frozenset(TASK_STATE_EVENTS)


@dataclass(frozen=True, slots=True)
class ManufacturerMaintenanceTask:
    """One catalog entry with its verbatim manufacturer cadence."""

    task_id: str
    name: str
    description: str
    trigger_kind: str
    cadence: str
    notes: str
    interval_months: int | None = None
    interval_months_by_mode: Mapping[str, int] = field(default_factory=dict)
    prerequisite_task_ids: tuple[str, ...] = ()
    warning_days: int = 0
    source: str = MANUFACTURER_SOURCE
    source_url: str = MANUFACTURER_SOURCE_URL
    source_revision: str = MANUFACTURER_SOURCE_REVISION


# Manufacturer catalog. Cadence strings paraphrase the wiki minimally so the
# dashboard can show exactly what Bambu Lab published.
X2D_MAINTENANCE_TASKS: tuple[ManufacturerMaintenanceTask, ...] = (
    ManufacturerMaintenanceTask(
        task_id="x2d_xy_axis_clean_lubricate",
        name="Clean and lubricate the X and Y axes",
        description=(
            "Wipe the X-axis and both Y-axis linear shafts, inspect the belt "
            "surface, apply 1-2 drops of lubricating oil per 5 cm, and move the "
            "toolhead slowly to spread it."
        ),
        trigger_kind=TRIGGER_USAGE_TIERED_MONTHS,
        cadence=(
            "Every month at >= 5 printing hours/day, every 2 months at 1-5 "
            "printing hours/day, every 3 months below 1 printing hour/day"
        ),
        notes=(
            "Bambu Lab scales this interval by average daily printing time. The "
            "dashboard selects the tier from tracked print time."
        ),
        interval_months_by_mode={
            MODE_HEAVY_USE: 1,
            MODE_NORMAL: 2,
            MODE_LOW_USE: 3,
        },
        warning_days=7,
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_z_axis_deep_maintenance",
        name="Z-axis deep maintenance",
        description=(
            "Lower the heatbed, clean the three lead screws and the left/right "
            "linear rails, apply grease to the lead screws and oil to the rails, "
            "then cycle the heatbed to spread it."
        ),
        trigger_kind=TRIGGER_USAGE_TIERED_MONTHS,
        cadence=(
            "Every 3 months at >= 5 printing hours/day, every 4 months at 1-5 "
            "printing hours/day, every 5 months below 1 printing hour/day"
        ),
        notes=(
            "Bambu Lab calls this deep maintenance on the Z axis and scales it "
            "by average daily printing time."
        ),
        interval_months_by_mode={
            MODE_HEAVY_USE: 3,
            MODE_NORMAL: 4,
            MODE_LOW_USE: 5,
        },
        warning_days=14,
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_full_calibration_after_axis_service",
        name="Full calibration after axis service",
        description=(
            "After cleaning and lubricating the XYZ axes, run a full calibration "
            "on the printer screen: motor noise cancellation, vibration "
            "compensation, and auto bed leveling."
        ),
        trigger_kind=TRIGGER_EVENT_AFTER_TASK,
        cadence="Immediately after XY-axis or Z-axis maintenance is performed",
        notes=(
            "Event-driven, not scheduled. It becomes due when an axis "
            "maintenance completion is recorded after the last calibration."
        ),
        prerequisite_task_ids=(
            "x2d_xy_axis_clean_lubricate",
            "x2d_z_axis_deep_maintenance",
        ),
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_live_view_camera_cleaning",
        name="Clean the live view camera",
        description=(
            "Volatile particle deposits accumulate on the live view camera lens "
            "and blur remote viewing."
        ),
        trigger_kind=TRIGGER_CALENDAR_MONTHS,
        cadence="Every 6 months",
        notes=(
            "Bambu Lab says to shorten this interval for highly volatile "
            "materials such as ABS but publishes no shortened number, so no "
            "material-based schedule is derived."
        ),
        interval_months=6,
        warning_days=14,
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_chamber_interior_cleaning",
        name="Clean inside the chamber",
        description=(
            "Brush and wipe the chamber floor, lining, walls, and the base areas "
            "of the lead screw and linear shafts so debris cannot jam XYZ motion."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="After extended use; no numeric interval published",
        notes=(
            "Bambu Lab lists this in the periodic guide without an interval. It "
            "is commonly performed together with axis maintenance."
        ),
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_build_plate_cleaning",
        name="Clean the build plate",
        description=(
            "Clean the Bambu Textured PEI Plate and avoid touching the surface "
            "afterwards, because skin oils reduce adhesion."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="Regularly; no numeric interval published",
        notes="Bambu Lab says cleaning it regularly is recommended.",
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_main_extruder_cleaning",
        name="Clean and lubricate the main extruder",
        description=(
            "Quick clean with compressed air through the filament inlet, or deep "
            "clean the extruder housing and grease the gear transmission areas."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="After prolonged use; no numeric interval published",
        notes=(
            "Bambu Lab describes accumulated debris after prolonged use without "
            "publishing an interval."
        ),
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_activated_carbon_filter",
        name="Replace the activated carbon filter",
        description=(
            "Replace the activated carbon filter and clean the filter cover; dry "
            "the cover completely before refitting it."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="When heavily contaminated; no numeric interval published",
        notes=(
            "The X2D page states a contamination condition, not a schedule. "
            "Intervals published for other Bambu Lab models are not applied here."
        ),
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_silicone_nozzle_wiper",
        name="Replace the silicone nozzle wiper",
        description=(
            "Replace the silicone nozzle wiper if it is damaged or deformed so "
            "nozzle cleaning stays effective."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="When damaged or deformed; no numeric interval published",
        notes="Condition-based replacement.",
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_auxiliary_ptfe_tube",
        name="Replace the auxiliary extruder / right hotend PTFE tube",
        description=(
            "If the PTFE tube still moves while its locking nut is secured, the "
            "clamped end is worn and the tube should be replaced."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence="When the clamped end is worn; no numeric interval published",
        notes="Condition-based replacement checked by hand.",
    ),
    ManufacturerMaintenanceTask(
        task_id="x2d_filament_cutter_blade",
        name="Check the toolhead filament cutter blade",
        description=(
            "Inspect the toolhead filament cutter blade for dullness and replace "
            "it if cutting resistance has increased."
        ),
        trigger_kind=TRIGGER_MANUAL_INSPECTION,
        cadence=(
            "Every 8-12 rolls of regular filament, or every 6-10 rolls of "
            "high-wear filament such as PA+CF, PA+GF, PPA+CF"
        ),
        notes=(
            "The manufacturer cadence is measured in filament rolls. This "
            "deployment has no reliable consumed-roll counter (print weights are "
            "slicer estimates and spool sizes are unknown), so the rule is shown "
            "for manual tracking and is deliberately not scheduled automatically."
        ),
    ),
)


class MaintenanceNotifier(Protocol):
    """Outbound delivery contract for durable maintenance events.

    `deliver` returns the delivery status recorded against the event. It must
    never raise; the store is the source of truth and a failed delivery stays
    re-deliverable.
    """

    def deliver(self, event: Mapping[str, Any]) -> str: ...


class LoggingMaintenanceNotifier:
    """Default notifier: records the event in the service log only.

    No Home Assistant, Telegram, or push credential is used or required. A real
    outbound transport can replace this by implementing `MaintenanceNotifier`.
    """

    status = "logged"

    def deliver(self, event: Mapping[str, Any]) -> str:
        LOGGER.info(
            "printer maintenance event: type=%s subject=%s %s -> %s",
            event.get("event_type"),
            event.get("subject_id"),
            event.get("previous_state"),
            event.get("new_state"),
        )
        return self.status


def add_months(value: datetime, months: int) -> datetime:
    """Add whole calendar months, clamping to the shortest month length."""

    total = value.month - 1 + int(months)
    year = value.year + total // 12
    month = total % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def maintenance_mode(
    hours_per_day: float | None,
    *,
    history_days: float | None,
    minimum_history_days: int = MINIMUM_MODE_HISTORY_DAYS,
) -> tuple[str, str]:
    """Map tracked utilization onto the manufacturer usage tiers."""

    if hours_per_day is None or history_days is None:
        return MODE_NORMAL, "insufficient_tracked_history"
    if history_days < minimum_history_days:
        return MODE_NORMAL, "insufficient_tracked_history"
    if hours_per_day >= HEAVY_USE_HOURS_PER_DAY:
        return MODE_HEAVY_USE, "tracked_average_at_or_above_5_print_hours_per_day"
    if hours_per_day < LOW_USE_HOURS_PER_DAY:
        return MODE_LOW_USE, "tracked_average_below_1_print_hour_per_day"
    return MODE_NORMAL, "tracked_average_between_1_and_5_print_hours_per_day"


def task_status(
    task: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    usage: Mapping[str, Any],
    *,
    now: datetime,
    mode: str,
    last_completion_by_task: Mapping[str, datetime],
) -> dict[str, Any]:
    """Compute one task's public status for the given trigger kind."""

    trigger_kind = str(task["trigger_kind"] or TRIGGER_THRESHOLD)
    last = events[0] if events else None
    last_completed_at = _datetime(last["completed_at_utc"]) if last else None
    common = {
        "maintenance_task_id": task["task_id"],
        "name": task["name"],
        "description": task["description"],
        "enabled": bool(task["enabled"]),
        "due_when": task["due_when"],
        "trigger_kind": trigger_kind,
        "cadence": task["cadence"],
        "notes": task["notes"],
        "provenance": task["source"],
        "manufacturer_source": task["source"],
        "manufacturer_source_url": task["source_url"],
        "manufacturer_source_revision": task["source_revision"],
        "warning_source": task["warning_source"],
        "last_completed_at": last["completed_at_utc"] if last else None,
        "usage_hours_at_last_completion": (
            last["effective_usage_hours"] if last else None
        ),
        "print_count_at_last_completion": (
            last["completed_print_count"] if last else None
        ),
        "completion_count": len(events),
        "local_record_only": True,
        "printer_control": False,
        "prerequisite_task_ids": _json_list(task["prerequisite_task_ids"]),
        "applied_interval_months": None,
        "maintenance_mode_applied": None,
        "next_due_at": None,
        "remaining_days": None,
        "remaining_hours": None,
        "remaining_prints": None,
        "baseline_required": False,
    }

    if trigger_kind == TRIGGER_MANUAL_INSPECTION:
        return {
            **common,
            "state": STATE_ADVISORY,
            "due": False,
            "overdue": False,
            "warning": False,
            "triggers": [
                {
                    "trigger_type": TRIGGER_MANUAL_INSPECTION,
                    "interval": None,
                    "warning_threshold": None,
                    "current_accumulated_value": None,
                    "remaining": None,
                    "next_due_value": None,
                    "next_due_at": None,
                    "due": False,
                    "overdue": False,
                    "warning": False,
                }
            ],
        }

    if trigger_kind == TRIGGER_EVENT_AFTER_TASK:
        prerequisites = common["prerequisite_task_ids"]
        triggered_at = _latest_prerequisite(
            prerequisites, last_completion_by_task, after=last_completed_at
        )
        due = triggered_at is not None
        return {
            **common,
            "state": STATE_DUE if due else STATE_OK,
            "due": due,
            "overdue": False,
            "warning": False,
            "triggered_by_completed_at": _iso(triggered_at),
            "triggers": [
                {
                    "trigger_type": TRIGGER_EVENT_AFTER_TASK,
                    "interval": None,
                    "warning_threshold": None,
                    "current_accumulated_value": None,
                    "remaining": None,
                    "next_due_value": None,
                    "next_due_at": None,
                    "due": due,
                    "overdue": False,
                    "warning": False,
                    "prerequisite_task_ids": prerequisites,
                }
            ],
        }

    if trigger_kind not in BASELINE_TRIGGER_KINDS:
        # Manual and event kinds already returned; anything else is evaluated
        # as a generic threshold task rather than trusted blindly.
        trigger_kind = TRIGGER_THRESHOLD
        common["trigger_kind"] = trigger_kind

    if last is None:
        # No local completion history: the dashboard cannot know whether the
        # physical work was already done, so the task must not read as overdue.
        months = (
            _interval_months(task, trigger_kind, mode)
            if trigger_kind in {TRIGGER_CALENDAR_MONTHS, TRIGGER_USAGE_TIERED_MONTHS}
            else None
        )
        return {
            **common,
            "state": STATE_BASELINE_REQUIRED,
            "baseline_required": True,
            "due": False,
            "overdue": False,
            "warning": False,
            "applied_interval_months": months,
            "maintenance_mode_applied": (
                mode if trigger_kind == TRIGGER_USAGE_TIERED_MONTHS else None
            ),
            "triggers": _baseline_triggers(task, trigger_kind, mode),
        }

    if trigger_kind in {TRIGGER_CALENDAR_MONTHS, TRIGGER_USAGE_TIERED_MONTHS}:
        months = _interval_months(task, trigger_kind, mode)
        base = last_completed_at or _datetime(task["created_at_utc"])
        next_due = add_months(base, months) if base and months else None
        remaining_days = (
            None if next_due is None else (next_due - now).total_seconds() / 86400
        )
        warning_days = float(task["warning_days"] or 0)
        due = remaining_days is not None and remaining_days <= 0
        overdue = remaining_days is not None and remaining_days < 0
        warning = (
            remaining_days is not None
            and 0 < remaining_days <= warning_days
            and warning_days > 0
        )
        trigger = {
            "trigger_type": trigger_kind,
            "interval": months,
            "interval_unit": "calendar_months",
            "warning_threshold": warning_days,
            "warning_threshold_unit": "days",
            "current_accumulated_value": (
                None if base is None else round((now - base).total_seconds() / 86400, 4)
            ),
            "remaining": None if remaining_days is None else round(remaining_days, 4),
            "remaining_unit": "days",
            "next_due_value": months,
            "next_due_at": _iso(next_due),
            "due": due,
            "overdue": overdue,
            "warning": warning,
        }
        if trigger_kind == TRIGGER_USAGE_TIERED_MONTHS:
            trigger["maintenance_mode_applied"] = mode
            trigger["interval_months_by_mode"] = _mode_intervals(task)
        return {
            **common,
            "state": _state(due, overdue, warning),
            "due": due,
            "overdue": overdue,
            "warning": warning,
            "applied_interval_months": months,
            "maintenance_mode_applied": (
                mode if trigger_kind == TRIGGER_USAGE_TIERED_MONTHS else None
            ),
            "next_due_at": _iso(next_due),
            "remaining_days": None
            if remaining_days is None
            else round(remaining_days, 4),
            "triggers": [trigger],
        }

    return _threshold_status(task, last, usage, common, now=now)


def _threshold_status(
    task: Mapping[str, Any],
    last: Mapping[str, Any],
    usage: Mapping[str, Any],
    common: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    base_hours = float(last["effective_usage_hours"])
    base_prints = int(last["completed_print_count"])
    base_date = _datetime(last["completed_at_utc"])
    current_hours = max(
        0.0, float(usage["maintenance_effective_lifetime_hours"]) - base_hours
    )
    current_prints = max(
        0, int(usage["locally_observed_completed_print_count"]) - base_prints
    )
    current_days = (
        max(0.0, (now - base_date).total_seconds() / 86400) if base_date else 0.0
    )
    metrics: list[dict[str, Any]] = []
    remaining_by_kind: dict[str, float] = {}
    for kind, current, interval, warning_threshold in (
        (
            "operating_hours",
            current_hours,
            task["interval_hours"],
            task["warning_hours"],
        ),
        (
            "completed_prints",
            current_prints,
            task["interval_prints"],
            task["warning_prints"],
        ),
        (
            "calendar_days",
            current_days,
            task["interval_days"],
            task["warning_days"],
        ),
    ):
        if interval is None:
            continue
        remaining = float(interval) - float(current)
        remaining_by_kind[kind] = remaining
        next_due_at = None
        if kind == "calendar_days" and base_date is not None:
            next_due_at = _iso(base_date + timedelta(days=float(interval)))
        metrics.append(
            {
                "trigger_type": kind,
                "interval": interval,
                "warning_threshold": warning_threshold,
                "current_accumulated_value": round(current, 4),
                "remaining": round(remaining, 4),
                "next_due_value": round(
                    (
                        base_hours
                        if kind == "operating_hours"
                        else base_prints
                        if kind == "completed_prints"
                        else 0
                    )
                    + float(interval),
                    4,
                ),
                "due": remaining <= 0,
                "overdue": remaining < 0,
                "warning": remaining > 0 and remaining <= float(warning_threshold),
                "next_due_at": next_due_at,
            }
        )
    due_flags = [bool(metric["due"]) for metric in metrics]
    due = all(due_flags) if task["due_when"] == "all" else any(due_flags)
    overdue_flags = [bool(metric["overdue"]) for metric in metrics]
    overdue = all(overdue_flags) if task["due_when"] == "all" else any(overdue_flags)
    warning = not due and any(
        bool(metric["warning"] or metric["due"]) for metric in metrics
    )
    calendar_metric = next(
        (item for item in metrics if item["trigger_type"] == "calendar_days"), None
    )
    return {
        **common,
        "state": _state(due, overdue, warning),
        "due": due,
        "overdue": overdue,
        "warning": warning,
        "triggers": metrics,
        "next_due_at": calendar_metric["next_due_at"] if calendar_metric else None,
        "remaining_days": (
            round(remaining_by_kind["calendar_days"], 4)
            if "calendar_days" in remaining_by_kind
            else None
        ),
        "remaining_hours": (
            round(remaining_by_kind["operating_hours"], 4)
            if "operating_hours" in remaining_by_kind
            else None
        ),
        "remaining_prints": (
            round(remaining_by_kind["completed_prints"], 4)
            if "completed_prints" in remaining_by_kind
            else None
        ),
    }


def maintenance_summary(
    tasks: Sequence[Mapping[str, Any]], *, usage: Mapping[str, Any]
) -> dict[str, Any]:
    """Aggregate the per-task states into the dashboard header contract."""

    enabled = [task for task in tasks if task.get("enabled")]
    counts = {
        state: sum(1 for task in enabled if task.get("state") == state)
        for state in (
            STATE_OK,
            STATE_DUE_SOON,
            STATE_DUE,
            STATE_OVERDUE,
            STATE_BASELINE_REQUIRED,
            STATE_ADVISORY,
        )
    }
    overall = STATE_OK
    for task in enabled:
        state = str(task.get("state") or STATE_OK)
        if STATE_SEVERITY.get(state, 0) > STATE_SEVERITY.get(overall, 0):
            overall = state
    # "Next" only means something for a task that is either already actionable
    # or has a computable remaining interval.
    scheduled = [
        task
        for task in enabled
        if task.get("state") in {STATE_DUE_SOON, STATE_DUE, STATE_OVERDUE}
        or (task.get("state") == STATE_OK and _has_remaining(task))
    ]
    next_task = _next_task(scheduled)
    return {
        "overall_state": overall if enabled else STATE_OK,
        "task_count": len(enabled),
        "counts": counts,
        "due_soon_count": counts[STATE_DUE_SOON],
        "due_count": counts[STATE_DUE],
        "overdue_count": counts[STATE_OVERDUE],
        "baseline_required_count": counts[STATE_BASELINE_REQUIRED],
        "advisory_count": counts[STATE_ADVISORY],
        "next_task": next_task,
        "maintenance_mode": usage.get("maintenance_mode"),
        "maintenance_mode_reason": usage.get("maintenance_mode_reason"),
        "maintenance_mode_source": usage.get("maintenance_mode_source"),
        "rolling_print_hours_per_day": usage.get("rolling_tracked_print_hours_per_day"),
        "local_record_only": True,
        "printer_control": False,
    }


def _has_remaining(task: Mapping[str, Any]) -> bool:
    if isinstance(task.get("remaining_days"), (int, float)):
        return True
    return any(
        isinstance(trigger.get("remaining"), (int, float))
        for trigger in task.get("triggers", [])
    )


def _next_task(tasks: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    ranked: list[tuple[int, float, Mapping[str, Any]]] = []
    for task in tasks:
        severity = -STATE_SEVERITY.get(str(task.get("state") or STATE_OK), 0)
        remaining = task.get("remaining_days")
        if remaining is None:
            remaining = min(
                (
                    float(trigger["remaining"])
                    for trigger in task.get("triggers", [])
                    if isinstance(trigger.get("remaining"), (int, float))
                ),
                default=float("inf"),
            )
        ranked.append((severity, float(remaining), task))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("name"))))
    chosen = ranked[0][2]
    return {
        "maintenance_task_id": chosen.get("maintenance_task_id"),
        "name": chosen.get("name"),
        "state": chosen.get("state"),
        "next_due_at": chosen.get("next_due_at"),
        "remaining_days": chosen.get("remaining_days"),
        "cadence": chosen.get("cadence"),
    }


def _baseline_triggers(
    task: Mapping[str, Any], trigger_kind: str, mode: str
) -> list[dict[str, Any]]:
    if trigger_kind in {TRIGGER_CALENDAR_MONTHS, TRIGGER_USAGE_TIERED_MONTHS}:
        months = _interval_months(task, trigger_kind, mode)
        trigger = {
            "trigger_type": trigger_kind,
            "interval": months,
            "interval_unit": "calendar_months",
            "warning_threshold": float(task["warning_days"] or 0),
            "warning_threshold_unit": "days",
            "current_accumulated_value": None,
            "remaining": None,
            "next_due_value": months,
            "next_due_at": None,
            "due": False,
            "overdue": False,
            "warning": False,
        }
        if trigger_kind == TRIGGER_USAGE_TIERED_MONTHS:
            trigger["maintenance_mode_applied"] = mode
            trigger["interval_months_by_mode"] = _mode_intervals(task)
        return [trigger]
    triggers = []
    for kind, interval, warning_threshold, unit in (
        ("operating_hours", task["interval_hours"], task["warning_hours"], "hours"),
        ("completed_prints", task["interval_prints"], task["warning_prints"], "prints"),
        ("calendar_days", task["interval_days"], task["warning_days"], "days"),
    ):
        if interval is None:
            continue
        triggers.append(
            {
                "trigger_type": kind,
                "interval": interval,
                "warning_threshold": warning_threshold,
                "current_accumulated_value": None,
                "remaining": None,
                "next_due_value": None,
                "next_due_at": None,
                "due": False,
                "overdue": False,
                "warning": False,
                "interval_unit": unit,
            }
        )
    return triggers


def _interval_months(
    task: Mapping[str, Any], trigger_kind: str, mode: str
) -> int | None:
    if trigger_kind == TRIGGER_CALENDAR_MONTHS:
        interval = task["interval_months"]
        return None if interval is None else int(interval)
    intervals = _mode_intervals(task)
    value = intervals.get(mode) or intervals.get(MODE_NORMAL)
    return None if value is None else int(value)


def _mode_intervals(task: Mapping[str, Any]) -> dict[str, int]:
    return {
        mode: int(task[column])
        for mode, column in (
            (MODE_LOW_USE, "interval_months_low_use"),
            (MODE_NORMAL, "interval_months_normal_use"),
            (MODE_HEAVY_USE, "interval_months_heavy_use"),
        )
        if task[column] is not None
    }


def _latest_prerequisite(
    prerequisites: Sequence[str],
    last_completion_by_task: Mapping[str, datetime],
    *,
    after: datetime | None,
) -> datetime | None:
    candidates = [
        completed
        for task_id in prerequisites
        if (completed := last_completion_by_task.get(str(task_id))) is not None
        and (after is None or completed > after)
    ]
    return max(candidates) if candidates else None


def _state(due: bool, overdue: bool, warning: bool) -> str:
    if overdue:
        return STATE_OVERDUE
    if due:
        return STATE_DUE
    if warning:
        return STATE_DUE_SOON
    return STATE_OK


def _json_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)
