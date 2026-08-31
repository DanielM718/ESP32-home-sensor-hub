"""Flask REST API for the home sensor dashboard."""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, current_app, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.config import AppSettings, ConfigError, configure_logging, load_settings
from app.export_queries import InfluxExportQueryRepository
from app.persistence import MonitoringExportStore
from app.printer_intelligence import PrinterIntelligenceError
from app.printer_queries import (
    PrinterReadRepository,
    printer_telemetry_query_from_params,
)
from app.queries import (
    InfluxReadRepository,
    QueryValidationError,
    latest_with_air_quality_context,
    latest_with_node_status,
    readings_query_from_params,
)
from app.service_status import SystemStatusProvider
from app.workflow_routes import register_workflow_routes
from app.workflow_services import ExportService, MonitoringService, utc_now
from app.workflows import (
    WorkflowConflictError,
    WorkflowNotFoundError,
    WorkflowValidationError,
)

LOGGER = logging.getLogger("home_sensor.web")
SERVER_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = SERVER_ROOT / "frontend"


def create_app(
    settings: AppSettings | None = None,
    repository: Any | None = None,
    monitoring_store: MonitoringExportStore | None = None,
    export_query_repository: Any | None = None,
    clock: Any | None = None,
    status_provider: Any | None = None,
    printer_repository: Any | None = None,
) -> Flask:
    """Create the Flask WSGI application."""

    try:
        settings = settings or load_settings()
        configure_logging(settings.log_level)
    except ConfigError:
        logging.basicConfig(level=logging.ERROR)
        raise

    app = Flask(
        __name__,
        static_folder=str(FRONTEND_DIR / "static"),
        static_url_path="/static",
        template_folder=str(FRONTEND_DIR / "templates"),
    )
    read_repository = repository or InfluxReadRepository(
        settings.influx,
        expected_publish_seconds=settings.air_quality.expected_publish_seconds,
        minimum_coverage_percent=settings.air_quality.rolling_minimum_coverage_percent,
    )
    app.config["REPOSITORY"] = read_repository
    app.config["PRINTER_REPOSITORY"] = printer_repository or PrinterReadRepository(
        settings.influx,
        database_path=Path(
            os.environ.get("PRINTER_DB_PATH", "/var/lib/home-sensor/printer.sqlite3")
        ),
        environment_location=os.environ.get("PRINTER_ENVIRONMENT_LOCATION", "office"),
        baseline_minutes=int(os.environ.get("PRINTER_BASELINE_MINUTES", "30")),
        recovery_minutes=int(os.environ.get("PRINTER_RECOVERY_MINUTES", "120")),
        raw_retention_seconds=settings.monitoring_exports.raw_retention_seconds,
        rolling_window_days=int(
            os.environ.get("PRINTER_MAINTENANCE_ROLLING_WINDOW_DAYS", "30")
        ),
        minimum_mode_history_days=int(
            os.environ.get("PRINTER_MAINTENANCE_MINIMUM_HISTORY_DAYS", "7")
        ),
    )
    app.config["NODE_STALE_AFTER_SECONDS"] = settings.node_stale_after_seconds
    app.config["AIR_QUALITY_STALE_AFTER_SECONDS"] = (
        settings.air_quality.stale_after_seconds
    )
    app.config["MONITORING_MAX_DURATION_SECONDS"] = (
        settings.monitoring_exports.raw_retention_seconds
    )
    app.config["STATUS_PROVIDER"] = status_provider or SystemStatusProvider()
    app.config["SEN66_EXPECTED_PUBLISH_SECONDS"] = (
        settings.air_quality.expected_publish_seconds
    )

    store = monitoring_store or MonitoringExportStore(
        settings.monitoring_exports.database_path,
        settings.monitoring_exports.output_dir,
    )
    store.initialize()
    app.config["MONITORING_STORE"] = store
    export_queries = export_query_repository or InfluxExportQueryRepository(
        settings.influx,
        raw_retention_seconds=settings.monitoring_exports.raw_retention_seconds,
    )
    current_clock = clock or utc_now
    export_service = ExportService(
        store,
        clock=current_clock,
        raw_retention_seconds=settings.monitoring_exports.raw_retention_seconds,
        capability_resolver=read_repository.latest,
    )
    app.config["EXPORT_SERVICE"] = export_service
    app.config["MONITORING_SERVICE"] = MonitoringService(
        store,
        export_service,
        export_queries,
        clock=current_clock,
        max_duration_seconds=settings.monitoring_exports.raw_retention_seconds,
        capability_resolver=read_repository.latest,
    )

    register_routes(app)
    register_workflow_routes(app)
    register_error_handlers(app)
    return app


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index() -> Any:
        return render_template("index.html")

    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"status": "ok"})

    @app.get("/api/latest")
    def latest() -> Any:
        repository = _repository()
        context_method = getattr(repository, "air_quality_context", None)
        if callable(context_method):
            with ThreadPoolExecutor(max_workers=2) as executor:
                latest_future = executor.submit(repository.latest)
                context_future = executor.submit(context_method)
                latest_payload = latest_future.result()
                context = context_future.result()
        else:
            latest_payload = repository.latest()
            context = {"locations": {}}
        latest_payload = latest_with_air_quality_context(
            latest_payload,
            context,
            stale_after_seconds=int(
                current_app.config["AIR_QUALITY_STALE_AFTER_SECONDS"]
            ),
        )
        stale_after_seconds = int(current_app.config["NODE_STALE_AFTER_SECONDS"])
        return jsonify(
            latest_with_node_status(
                latest_payload,
                stale_after_seconds=stale_after_seconds,
                air_quality_stale_after_seconds=int(
                    current_app.config["AIR_QUALITY_STALE_AFTER_SECONDS"]
                ),
            )
        )

    @app.get("/api/readings")
    def readings() -> Any:
        query = readings_query_from_params(request.args)
        return jsonify(_repository().readings(query))

    @app.get("/api/nodes")
    def nodes() -> Any:
        stale_after_seconds = int(current_app.config["NODE_STALE_AFTER_SECONDS"])
        return jsonify(
            _repository().nodes(
                stale_after_seconds=stale_after_seconds,
                air_quality_stale_after_seconds=int(
                    current_app.config["AIR_QUALITY_STALE_AFTER_SECONDS"]
                ),
            )
        )

    @app.get("/api/status")
    def status() -> Any:
        payload = dict(current_app.config["STATUS_PROVIDER"].snapshot())
        payload["configuration"] = {
            "node_stale_after_seconds": int(
                current_app.config["NODE_STALE_AFTER_SECONDS"]
            ),
            "air_quality_stale_after_seconds": int(
                current_app.config["AIR_QUALITY_STALE_AFTER_SECONDS"]
            ),
            "sen66_expected_publish_seconds": int(
                current_app.config["SEN66_EXPECTED_PUBLISH_SECONDS"]
            ),
            "raw_retention_seconds": int(
                current_app.config["MONITORING_MAX_DURATION_SECONDS"]
            ),
            "stored_air_quality_resolution_seconds": 15 * 60,
        }
        return jsonify(payload)

    @app.get("/api/printer")
    def printer() -> Any:
        payload = dict(current_app.config["PRINTER_REPOSITORY"].current())
        printer_id = payload.get("printer_id")
        payload["sen66_monitoring"] = (
            current_app.config["MONITORING_STORE"].printer_monitoring_status(
                printer_id=str(printer_id)
            )
            if printer_id
            else None
        )
        return jsonify(payload)

    @app.get("/api/printer/sessions")
    def printer_sessions() -> Any:
        raw_limit = request.args.get("limit", "20")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise QueryValidationError("limit must be an integer") from exc
        if not 1 <= limit <= 100:
            raise QueryValidationError("limit must be between 1 and 100")
        return jsonify(current_app.config["PRINTER_REPOSITORY"].sessions(limit=limit))

    @app.get("/api/printer/sessions/<history_id>")
    def printer_session(history_id: str) -> Any:
        item = current_app.config["PRINTER_REPOSITORY"].history_item(history_id)
        if item is None:
            return jsonify(
                {"error": "not_found", "message": "unknown print session"}
            ), 404
        return jsonify(item)

    @app.get("/api/printer/history")
    def printer_history() -> Any:
        raw_limit = request.args.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise QueryValidationError("limit must be an integer") from exc
        if not 1 <= limit <= 500:
            raise QueryValidationError("limit must be between 1 and 500")
        return jsonify(current_app.config["PRINTER_REPOSITORY"].history(limit=limit))

    @app.get("/api/printer/telemetry")
    def printer_telemetry() -> Any:
        query = printer_telemetry_query_from_params(request.args)
        return jsonify(current_app.config["PRINTER_REPOSITORY"].telemetry(query))

    @app.get("/api/printer/usage")
    def printer_usage() -> Any:
        return jsonify(current_app.config["PRINTER_REPOSITORY"].usage())

    @app.get("/api/printer/maintenance")
    def printer_maintenance() -> Any:
        return jsonify(current_app.config["PRINTER_REPOSITORY"].maintenance())

    @app.get("/api/printer/maintenance/events")
    def printer_maintenance_events() -> Any:
        raw_limit = request.args.get("limit", "100")
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise QueryValidationError("limit must be an integer") from exc
        if not 1 <= limit <= 500:
            raise QueryValidationError("limit must be between 1 and 500")
        pending = request.args.get("pending", "false").lower()
        if pending not in {"true", "false"}:
            raise QueryValidationError("pending must be true or false")
        return jsonify(
            current_app.config["PRINTER_REPOSITORY"].maintenance_events(
                limit=limit, pending_only=pending == "true"
            )
        )

    @app.post("/api/printer/maintenance/complete-all")
    def complete_all_printer_maintenance() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise QueryValidationError(
                "confirm=true is required; this records local maintenance only"
            )
        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            raise QueryValidationError("notes must be a string")
        result = current_app.config["PRINTER_REPOSITORY"].complete_all_maintenance(
            notes=notes,
            completed_at=datetime.now(timezone.utc),
        )
        return jsonify(result), 201

    @app.post("/api/printer/maintenance/<task_id>/complete")
    def complete_printer_maintenance(task_id: str) -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise QueryValidationError(
                "confirm=true is required; this records local maintenance only"
            )
        notes = payload.get("notes", "")
        if not isinstance(notes, str):
            raise QueryValidationError("notes must be a string")
        result = current_app.config["PRINTER_REPOSITORY"].complete_maintenance(
            task_id,
            notes=notes,
            completed_at=datetime.now(timezone.utc),
        )
        return jsonify(result), 201

    @app.get("/api/printer/environment-summary")
    def printer_environment_summary() -> Any:
        repository = current_app.config["PRINTER_REPOSITORY"]
        session_id = request.args.get("session_id")
        return jsonify(
            repository.environment_summary(session_id)
            if session_id
            else repository.environment_summary()
        )


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(WorkflowValidationError)
    def workflow_validation_error(exc: WorkflowValidationError) -> Any:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    @app.errorhandler(WorkflowNotFoundError)
    def workflow_not_found_error(exc: WorkflowNotFoundError) -> Any:
        return jsonify({"error": "not_found", "message": str(exc)}), 404

    @app.errorhandler(WorkflowConflictError)
    def workflow_conflict_error(exc: WorkflowConflictError) -> Any:
        return jsonify({"error": "conflict", "message": str(exc)}), 409

    @app.errorhandler(QueryValidationError)
    def query_validation_error(exc: QueryValidationError) -> Any:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    @app.errorhandler(PrinterIntelligenceError)
    def printer_intelligence_error(exc: PrinterIntelligenceError) -> Any:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> Any:
        return jsonify({"error": exc.name, "message": exc.description}), exc.code

    @app.errorhandler(Exception)
    def unhandled_error(exc: Exception) -> Any:
        LOGGER.exception("API request failed: %s", exc)
        return jsonify(
            {"error": "service_unavailable", "message": "backend query failed"}
        ), 503


def _repository() -> Any:
    return current_app.config["REPOSITORY"]
