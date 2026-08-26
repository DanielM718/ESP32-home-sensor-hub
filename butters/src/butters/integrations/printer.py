"""Bounded read-only access to dashboard printer endpoints."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from butters.assistant_config import IntegrationSettings
from butters.integrations.model import (
    IntegrationError,
    PrintEnvironmentSnapshot,
    PrinterIntelligenceSnapshot,
    PrinterSession,
    PrinterSnapshot,
)

OpenUrl = Callable[..., Any]

USAGE_FIELDS = frozenset(
    {
        "tracked_print_seconds",
        "tracked_print_hours",
        "tracked_job_count",
        "tracked_completed_count",
        "tracked_failed_or_cancelled_count",
        "tracked_unknown_result_count",
        "tracked_unknown_interval_job_count",
        "tracked_first_print_at",
        "tracked_last_print_at",
        "tracked_history_complete",
        "tracked_history_completeness_reasons",
        "tracked_history_provenance",
        "rolling_window_days",
        "rolling_tracked_print_hours",
        "rolling_tracked_history_days",
        "rolling_tracked_print_hours_per_day",
        "maintenance_mode",
        "maintenance_mode_reason",
        "maintenance_mode_source",
    }
)
MAINTENANCE_TASK_FIELDS = frozenset(
    {
        "maintenance_task_id",
        "name",
        "enabled",
        "state",
        "baseline_required",
        "warning",
        "due",
        "overdue",
        "trigger_kind",
        "cadence",
        "next_due_at",
        "remaining_days",
        "manufacturer_source",
        "manufacturer_source_url",
        "manufacturer_source_revision",
        "warning_source",
        "last_completed_at",
        "completion_count",
        "applied_interval_months",
        "maintenance_mode_applied",
    }
)
MAINTENANCE_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "subject_type",
        "subject_id",
        "event_type",
        "previous_state",
        "new_state",
        "created_at",
        "delivery_status",
        "delivered_at",
    }
)


class DashboardPrinterAdapter:
    def __init__(
        self,
        settings: IntegrationSettings,
        *,
        opener: OpenUrl = urllib.request.urlopen,
    ) -> None:
        self.settings = settings
        self._opener = opener

    def current(self) -> PrinterSnapshot:
        payload = self._fetch("/api/printer")
        if payload.get("status") == "not_configured":
            raise IntegrationError("unavailable", "printer observer is not configured")
        printer_id = _required_string(payload, "printer_id")
        printer_model = _required_string(payload, "printer_model")
        state = _required_string(payload, "normalized_state")
        provenance = payload.get("provenance", {})
        if not isinstance(provenance, Mapping):
            provenance = {}
        return PrinterSnapshot(
            printer_id=printer_id,
            printer_model=printer_model,
            online=payload.get("online") is True,
            normalized_state=state,
            observed_at=_optional_string(payload.get("observed_at")),
            values=dict(payload),
            provenance={
                str(key): str(value)
                for key, value in provenance.items()
                if isinstance(key, str) and isinstance(value, str)
            },
        )

    def environment_summary(self) -> PrintEnvironmentSnapshot:
        payload = self._fetch("/api/printer/environment-summary")
        session = payload.get("session", {})
        metrics = payload.get("metrics", {})
        if not isinstance(session, Mapping) or not isinstance(metrics, Mapping):
            raise IntegrationError(
                "invalid_response", "printer summary has an invalid shape"
            )
        parsed_metrics: dict[str, dict[str, float | int | None]] = {}
        for metric, values in metrics.items():
            if not isinstance(metric, str) or not isinstance(values, Mapping):
                continue
            parsed_metrics[metric] = {
                str(key): value
                for key, value in values.items()
                if isinstance(key, str)
                and (
                    value is None
                    or (isinstance(value, (int, float)) and not isinstance(value, bool))
                )
            }
        recovery = payload.get("voc_recovery_seconds")
        return PrintEnvironmentSnapshot(
            available=payload.get("available") is True,
            reason=_optional_string(payload.get("reason")),
            observational=payload.get("observational") is True,
            session=dict(session),
            metrics=parsed_metrics,
            voc_recovery_seconds=(
                recovery
                if isinstance(recovery, int) and not isinstance(recovery, bool)
                else None
            ),
        )

    def intelligence(self) -> PrinterIntelligenceSnapshot:
        maintenance = self.maintenance()
        history = self._fetch("/api/printer/history?limit=100")
        prints = history.get("history", [])
        if not isinstance(prints, list):
            raise IntegrationError(
                "invalid_response", "printer intelligence has an invalid shape"
            )
        return PrinterIntelligenceSnapshot(
            usage=maintenance.usage,
            maintenance_tasks=maintenance.maintenance_tasks,
            completion_history=maintenance.completion_history,
            print_history=tuple(
                dict(item) for item in prints if isinstance(item, Mapping)
            ),
            maintenance_summary=maintenance.maintenance_summary,
            maintenance_notifications=maintenance.maintenance_notifications,
            manufacturer_source=maintenance.manufacturer_source,
        )

    def usage(self) -> dict[str, object]:
        payload = self._fetch("/api/printer/usage")
        usage = payload.get("usage", {})
        if not isinstance(usage, Mapping):
            raise IntegrationError("invalid_response", "printer usage is invalid")
        return _selected(usage, USAGE_FIELDS)

    def maintenance(self) -> PrinterIntelligenceSnapshot:
        payload = self._fetch("/api/printer/maintenance")
        usage = payload.get("usage", {})
        summary = payload.get("summary", {})
        tasks = payload.get("tasks", [])
        completions = payload.get("completion_history", [])
        notifications = payload.get("recent_notifications", [])
        manufacturer_source = payload.get("manufacturer_source", {})
        if (
            not isinstance(usage, Mapping)
            or not isinstance(summary, Mapping)
            or not isinstance(manufacturer_source, Mapping)
            or not all(
                isinstance(value, list) for value in (tasks, completions, notifications)
            )
        ):
            raise IntegrationError(
                "invalid_response", "printer maintenance has an invalid shape"
            )
        return PrinterIntelligenceSnapshot(
            usage=_selected(usage, USAGE_FIELDS),
            maintenance_tasks=tuple(
                _selected(item, MAINTENANCE_TASK_FIELDS)
                for item in tasks
                if isinstance(item, Mapping)
            ),
            completion_history=tuple(
                dict(item) for item in completions[:20] if isinstance(item, Mapping)
            ),
            print_history=(),
            maintenance_summary={str(key): value for key, value in summary.items()},
            maintenance_notifications=tuple(
                _selected(item, MAINTENANCE_EVENT_FIELDS)
                for item in notifications[:20]
                if isinstance(item, Mapping)
            ),
            manufacturer_source={
                str(key): value for key, value in manufacturer_source.items()
            },
        )

    def maintenance_events(self, limit: int = 20) -> tuple[dict[str, object], ...]:
        bounded = max(1, min(limit, 20))
        payload = self._fetch(
            f"/api/printer/maintenance/events?limit={bounded}&pending=false"
        )
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise IntegrationError(
                "invalid_response", "printer maintenance events are invalid"
            )
        return tuple(
            _selected(item, MAINTENANCE_EVENT_FIELDS)
            for item in events[:bounded]
            if isinstance(item, Mapping)
        )

    def current_session(self) -> PrinterSession | None:
        snapshot = self.current()
        if snapshot.normalized_state not in {
            "preparing",
            "printing",
            "paused",
            "finishing",
        }:
            return None
        return _session_from_mapping(
            snapshot.values,
            printer=snapshot.printer_id,
            fallback_id="current",
            fallback_status=snapshot.normalized_state,
            source="current_state",
        )

    def recent_sessions(self, limit: int) -> tuple[PrinterSession, ...]:
        bounded = max(1, min(limit, 20))
        payload = self._fetch(f"/api/printer/sessions?limit={bounded}")
        raw = payload.get("sessions", [])
        if not isinstance(raw, list):
            raise IntegrationError("invalid_response", "printer sessions are invalid")
        sessions = []
        for index, item in enumerate(raw[:bounded]):
            if isinstance(item, Mapping):
                sessions.append(
                    _session_from_mapping(
                        item,
                        printer=str(item.get("printer_id") or "x2d"),
                        fallback_id=f"session-{index + 1}",
                        fallback_status="completed"
                        if item.get("ended_at")
                        else "active",
                        source=str(item.get("source") or "local_observation"),
                    )
                )
        return tuple(sessions)

    def session(self, print_id: str) -> PrinterSession | None:
        # IDs come only from a bounded local result and are URL-safe opaque IDs.
        if (
            not print_id
            or len(print_id) > 128
            or not all(
                character.isalnum() or character in "-_" for character in print_id
            )
        ):
            raise IntegrationError("policy_denied", "print identifier is invalid")
        for item in self.recent_sessions(20):
            if item.print_id == print_id:
                return item
        return None

    def _fetch(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.dashboard_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "Butters/0.4"},
            method="GET",
        )
        try:
            with self._opener(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise IntegrationError(
                        "upstream_status", f"dashboard returned HTTP {status}"
                    )
                raw = response.read(self.settings.max_response_bytes + 1)
        except IntegrationError:
            raise
        except TimeoutError as exc:
            raise IntegrationError("timeout", "printer query timed out") from exc
        except urllib.error.HTTPError as exc:
            raise IntegrationError(
                "upstream_status", f"dashboard returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(
                "unavailable", "printer data is unavailable"
            ) from exc
        if len(raw) > self.settings.max_response_bytes:
            raise IntegrationError("invalid_response", "printer response was too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError(
                "invalid_response", "dashboard returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise IntegrationError(
                "invalid_response", "printer payload is not an object"
            )
        return payload


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise IntegrationError("invalid_response", f"printer payload has no {key}")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _selected(value: Mapping[str, Any], fields: frozenset[str]) -> dict[str, object]:
    return {str(key): item for key, item in value.items() if key in fields}


def _session_from_mapping(
    value: Mapping[str, Any],
    *,
    printer: str,
    fallback_id: str,
    fallback_status: str,
    source: str,
) -> PrinterSession:
    started = _optional_string(value.get("started_at")) or _optional_string(
        value.get("print_started_at")
    )
    ended = _optional_string(value.get("ended_at")) or _optional_string(
        value.get("print_finished_at")
    )
    duration = _integer(value.get("duration_seconds"))
    progress = _number(value.get("progress_percent"))
    temperatures = {}
    for key in (
        "nozzle_1_temperature",
        "nozzle_2_temperature",
        "bed_temperature",
        "chamber_temperature",
    ):
        number = _number(value.get(key))
        if number is not None:
            temperatures[key] = number
    return PrinterSession(
        printer=printer,
        print_id=str(value.get("session_id") or value.get("history_id") or fallback_id),
        job_id=_optional_string(value.get("job_id")),
        filename=_optional_string(value.get("job_name"))
        or _optional_string(value.get("filename")),
        started_at=started,
        ended_at=ended,
        duration_seconds=duration,
        progress_percent=progress,
        material=_optional_string(value.get("material"))
        or _optional_string(value.get("active_material")),
        status=str(
            value.get("result") or value.get("normalized_state") or fallback_status
        ),
        temperatures=temperatures,
        source=source,
    )


def _number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _integer(value: object) -> int | None:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )
