"""Idempotent, availability-aware bridge from prints to Active Monitoring."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from app.persistence import MonitoringExportStore
from app.printer_model import PrintSession
from app.queries import AIR_QUALITY_FIELDS

LOGGER = logging.getLogger("home_sensor.printer.monitoring")
SensorStatusProvider = Callable[[str, datetime], Mapping[str, Any]]


class PrinterMonitoringCoordinator:
    def __init__(
        self,
        store: MonitoringExportStore,
        *,
        environment_location: str,
        recovery_minutes: int,
        sensor_status_provider: SensorStatusProvider,
        maximum_interval_seconds: int = 72 * 60 * 60,
    ) -> None:
        self.store = store
        self.environment_location = environment_location
        self.recovery_minutes = recovery_minutes
        self.sensor_status_provider = sensor_status_provider
        self.maximum_interval_seconds = maximum_interval_seconds

    def synchronize(
        self, session: PrintSession | None, *, observed_at: datetime | None = None
    ) -> dict[str, Any] | None:
        if session is None:
            return None
        now = _aware_utc(observed_at or session.updated_at)
        existing_monitoring = self.store.monitoring_for_printer_session(
            session.session_id
        )
        previous_decision = self.store.printer_monitoring_status(
            printer_session_id=session.session_id
        )

        # A skipped start remains skipped for this print. A later sensor return
        # is reported but does not silently mint a partial-session interval.
        if (
            existing_monitoring is None
            and previous_decision is not None
            and previous_decision.get("state") == "skipped"
        ):
            return previous_decision

        sensor = dict(self.sensor_status_provider(self.environment_location, now))
        sensor_status = str(sensor.get("status") or "unknown")
        sensor_last_seen = str(sensor["last_seen"]) if sensor.get("last_seen") else None

        if existing_monitoring is None and sensor_status != "online":
            decision = self._record_status(
                session,
                state="skipped",
                reason=f"required SEN66 at {self.environment_location} is {sensor_status}",
                sensor_status=sensor_status,
                sensor_last_seen=sensor_last_seen,
                observed_at=now,
            )
            LOGGER.warning(
                "automatic SEN66 monitoring skipped: printer_session=%s location=%s sensor_status=%s",
                session.session_id,
                self.environment_location,
                sensor_status,
            )
            return decision

        monitoring = existing_monitoring or self.store.ensure_printer_session(
            printer_session_id=session.session_id,
            name=f"Printer · {session.job_name or 'observed print'}",
            start_time=session.started_at,
            provisional_end_time=session.started_at
            + timedelta(seconds=self.maximum_interval_seconds),
            sources=[
                {
                    "sensor_type": "air_quality",
                    "location": self.environment_location,
                }
            ],
            fields=list(AIR_QUALITY_FIELDS),
        )
        state = (
            "completed"
            if session.ended_at is not None
            else "running"
            if sensor_status == "online"
            else "degraded"
        )
        reason = (
            None
            if sensor_status == "online"
            else f"required SEN66 at {self.environment_location} became {sensor_status}"
        )
        decision = self._record_status(
            session,
            state=state,
            reason=reason,
            sensor_status=sensor_status,
            sensor_last_seen=sensor_last_seen,
            observed_at=now,
        )
        if previous_decision is not None and previous_decision.get("state") != state:
            LOGGER.warning(
                "automatic SEN66 monitoring state changed: printer_session=%s state=%s sensor_status=%s",
                session.session_id,
                state,
                sensor_status,
            )

        if session.ended_at is not None:
            monitoring = self.store.finish_printer_session(
                session.session_id,
                print_ended_at=session.ended_at,
                recovery_end_time=session.ended_at
                + timedelta(minutes=self.recovery_minutes),
            )
        return {**(monitoring or {}), "sensor_monitoring": decision}

    def _record_status(
        self,
        session: PrintSession,
        *,
        state: str,
        reason: str | None,
        sensor_status: str,
        sensor_last_seen: str | None,
        observed_at: datetime,
    ) -> dict[str, Any]:
        return self.store.record_printer_monitoring_status(
            printer_session_id=session.session_id,
            printer_id=session.printer_id,
            state=state,
            reason=reason,
            sensor_location=self.environment_location,
            sensor_status=sensor_status,
            sensor_last_seen=sensor_last_seen,
            updated_at=observed_at,
        )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
