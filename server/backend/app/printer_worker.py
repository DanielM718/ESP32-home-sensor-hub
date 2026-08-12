"""Failure-isolated Home Assistant-to-Influx printer observer."""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from app.config import configure_logging, load_settings
from app.influx import InfluxWriter
from app.printer_adapter import (
    HomeAssistantPrinterAdapter,
    PrinterAdapterError,
    unavailable_printer_state,
)
from app.printer_config import load_printer_settings
from app.printer_model import print_session_point, printer_state_point
from app.printer_persistence import PrinterStore

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
