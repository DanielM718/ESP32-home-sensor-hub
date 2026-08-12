"""Idempotent bridge from print sessions to existing Active Monitoring."""

from __future__ import annotations

from datetime import timedelta

from app.persistence import MonitoringExportStore
from app.printer_model import PrintSession
from app.queries import AIR_QUALITY_FIELDS


class PrinterMonitoringCoordinator:
    def __init__(
        self,
        store: MonitoringExportStore,
        *,
        environment_location: str,
        recovery_minutes: int,
        maximum_interval_seconds: int = 72 * 60 * 60,
    ) -> None:
        self.store = store
        self.environment_location = environment_location
        self.recovery_minutes = recovery_minutes
        self.maximum_interval_seconds = maximum_interval_seconds

    def synchronize(self, session: PrintSession | None) -> dict | None:
        if session is None:
            return None
        monitoring = self.store.ensure_printer_session(
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
        if session.ended_at is not None:
            monitoring = self.store.finish_printer_session(
                session.session_id,
                print_ended_at=session.ended_at,
                recovery_end_time=session.ended_at
                + timedelta(minutes=self.recovery_minutes),
            )
        return monitoring
