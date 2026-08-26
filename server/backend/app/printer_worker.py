"""Failure-isolated Home Assistant-to-Influx printer observer."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import configure_logging, load_settings
from app.influx import InfluxWriter
from app.persistence import MonitoringExportStore
from app.printer_adapter import (
    HomeAssistantPrinterAdapter,
    PrinterAdapterError,
    unavailable_printer_state,
)
from app.printer_config import load_printer_settings
from app.printer_intelligence import (
    BambuCloudHistoryAdapter,
    PrinterIntelligenceError,
    PrinterIntelligenceStore,
)
from app.printer_maintenance import (
    X2D_MAINTENANCE_TASKS,
    LoggingMaintenanceNotifier,
)
from app.printer_model import print_session_point, printer_state_point
from app.printer_monitoring import PrinterMonitoringCoordinator
from app.printer_persistence import PrinterStore
from app.queries import InfluxReadRepository, air_quality_sensor_status

LOGGER = logging.getLogger("home_sensor.printer")
DEFAULT_CONFIG_PATH = Path("/etc/home-sensor/printer.toml")


def run() -> int:
    app_settings = load_settings()
    configure_logging(app_settings.log_level)
    config_path = Path(
        os.environ.get("PRINTER_CONFIG_PATH", str(DEFAULT_CONFIG_PATH))
    ).expanduser()
    settings = load_printer_settings(config_path)
    token = os.environ.get("HOME_ASSISTANT_TOKEN", "")
    adapter = HomeAssistantPrinterAdapter(settings, token)
    store = PrinterStore(
        settings.database_path,
        terminal_confirmations=settings.terminal_confirmations,
    )
    store.initialize()
    intelligence = PrinterIntelligenceStore(
        settings.database_path,
        rolling_window_days=settings.maintenance_rolling_window_days,
        minimum_mode_history_days=settings.maintenance_minimum_history_days,
    )
    intelligence.initialize()
    intelligence.sync_maintenance_tasks(
        settings.maintenance_tasks,
        now=datetime.now(timezone.utc),
        manufacturer_tasks=(
            X2D_MAINTENANCE_TASKS if settings.manufacturer_maintenance_enabled else ()
        ),
    )
    maintenance_notifier = LoggingMaintenanceNotifier()
    last_maintenance_evaluation = (
        time.monotonic() - settings.maintenance_evaluation_seconds
    )
    monitoring_coordinator = None
    if settings.automatic_monitoring:
        monitoring_store = MonitoringExportStore(
            settings.monitoring_database_path,
            settings.monitoring_output_dir,
        )
        monitoring_store.initialize()
        sensor_repository = InfluxReadRepository(
            app_settings.influx,
            expected_publish_seconds=app_settings.air_quality.expected_publish_seconds,
            minimum_coverage_percent=app_settings.air_quality.rolling_minimum_coverage_percent,
        )

        def required_sensor_status(location: str, observed_at: datetime) -> dict:
            try:
                latest = sensor_repository.latest()
            # Influx client versions expose several transport exception types;
            # this optional observer boundary converts all of them to UNKNOWN.
            except Exception:  # noqa: BLE001
                return {
                    "id": location,
                    "location": location,
                    "sensor_type": "air_quality",
                    "status": "unknown",
                    "last_seen": None,
                    "stale_reason": "availability_check_failed",
                }
            return air_quality_sensor_status(
                latest,
                location=location,
                stale_after_seconds=app_settings.air_quality.stale_after_seconds,
                observed_at=observed_at,
            )

        monitoring_coordinator = PrinterMonitoringCoordinator(
            monitoring_store,
            environment_location=settings.environment_location,
            recovery_minutes=settings.recovery_minutes,
            sensor_status_provider=required_sensor_status,
        )
    cloud_adapter = None
    cloud_token = os.environ.get("BAMBU_CLOUD_TOKEN", "")
    cloud_device_id = os.environ.get("BAMBU_DEVICE_ID", "")
    if settings.cloud_history_enabled and cloud_token and cloud_device_id:
        cloud_adapter = BambuCloudHistoryAdapter(
            cloud_token,
            cloud_device_id,
            timeout_seconds=settings.cloud_history_timeout_seconds,
            max_records=settings.cloud_history_max_records,
        )
    elif settings.cloud_history_enabled:
        LOGGER.warning(
            "Bambu Cloud history is disabled because its root-controlled environment is incomplete"
        )
    last_cloud_attempt = time.monotonic() - settings.cloud_history_refresh_seconds
    stop_event = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_args: stop_event.set())

    LOGGER.info(
        "starting read-only printer observer: printer=%s source=home_assistant poll=%.1fs",
        settings.printer_id,
        settings.poll_seconds,
    )
    failure_count = 0
    with InfluxWriter(app_settings.influx) as writer:
        while not stop_event.is_set():
            previous = store.current_state(settings.printer_id)
            try:
                state = adapter.fetch()
                failure_count = 0
            except PrinterAdapterError as exc:
                failure_count += 1
                state = unavailable_printer_state(settings, reason=str(exc))
                LOGGER.warning("printer observation unavailable: %s", exc)

            changed_sessions = store.process(state)
            intelligence.observe_usage(
                state.printer_id,
                observed_at=state.observed_at,
                ha_estimate_hours=state.ha_bambulab_estimated_usage_hours,
                printer_reported_hours=state.printer_reported_lifetime_hours,
            )
            if monitoring_coordinator is not None:
                try:
                    # Synchronizing the latest session every poll closes the
                    # crash window between a session transition and monitoring
                    # metadata update. The store is unique/idempotent by print UUID.
                    monitoring_coordinator.synchronize(
                        store.latest_session(settings.printer_id),
                        observed_at=state.observed_at,
                    )
                except Exception:
                    LOGGER.exception("printer active-monitoring synchronization failed")
            if (
                cloud_adapter is not None
                and time.monotonic() - last_cloud_attempt
                >= settings.cloud_history_refresh_seconds
            ):
                last_cloud_attempt = time.monotonic()
                attempted_at = datetime.now(timezone.utc)
                try:
                    cloud = cloud_adapter.fetch()
                    imported = intelligence.import_cloud_records(
                        settings.printer_id,
                        cloud["records"],
                        imported_at=attempted_at,
                        api_total=cloud["api_total"],
                        truncated=cloud["truncated"],
                    )
                    LOGGER.info(
                        "Bambu Cloud history read: records=%d inserted=%d updated=%d reconciled=%d",
                        cloud["records_retrieved"],
                        imported["inserted"],
                        imported["updated"],
                        imported["reconciled"],
                    )
                except PrinterIntelligenceError as exc:
                    intelligence.record_import_error(
                        settings.printer_id, str(exc), attempted_at=attempted_at
                    )
                    LOGGER.warning("Bambu Cloud history read unavailable: %s", exc)
            if (
                time.monotonic() - last_maintenance_evaluation
                >= settings.maintenance_evaluation_seconds
            ):
                last_maintenance_evaluation = time.monotonic()
                try:
                    # Edge-triggered: unchanged states append nothing, so a
                    # restart or a fast poll cannot repeat a notification.
                    events = intelligence.evaluate_maintenance_events(
                        settings.printer_id, now=state.observed_at
                    )
                    if events:
                        LOGGER.info(
                            "printer maintenance transitions recorded: %d", len(events)
                        )
                    intelligence.dispatch_notifications(
                        maintenance_notifier, now=state.observed_at
                    )
                except Exception:
                    LOGGER.exception("printer maintenance evaluation failed")
            try:
                writer.write_point_data(
                    printer_state_point(state, measurement="printer_state"),
                    bucket=app_settings.influx.live_bucket,
                )
                changed = (
                    previous is None
                    or previous.normalized_state != state.normalized_state
                    or previous.online != state.online
                    or previous.job_id != state.job_id
                )
                if changed or store.permanent_sample_due(
                    state.printer_id,
                    state.observed_at,
                    settings.permanent_sample_seconds,
                ):
                    writer.write_point_data(
                        printer_state_point(state, measurement="printer_state_5m")
                    )
                    store.mark_permanent_sample(state.printer_id, state.observed_at)
                for session in changed_sessions:
                    writer.write_point_data(print_session_point(session))
            except Exception:
                # This worker is non-critical. Never allow its Influx failure to
                # affect MQTT ingestion or any other service.
                LOGGER.exception("printer InfluxDB write failed")

            delay = min(
                settings.poll_seconds * (2 ** min(failure_count, 4)),
                300,
            )
            stop_event.wait(delay)
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
