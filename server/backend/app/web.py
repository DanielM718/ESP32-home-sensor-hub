"""Flask REST API for the home sensor dashboard."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from flask import Flask, current_app, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.config import AppSettings, ConfigError, configure_logging, load_settings
from app.export_queries import InfluxExportQueryRepository
from app.persistence import MonitoringExportStore
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
    export_queries = export_query_repository or InfluxExportQueryRepository(
        settings.influx
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
