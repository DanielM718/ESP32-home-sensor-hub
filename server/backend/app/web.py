"""Flask REST API for the home sensor dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, current_app, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from app.config import AppSettings, ConfigError, configure_logging, load_settings
from app.queries import (
    InfluxReadRepository,
    QueryValidationError,
    latest_with_node_status,
    readings_query_from_params,
)


LOGGER = logging.getLogger("home_sensor.web")
SERVER_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIR = SERVER_ROOT / "frontend"


def create_app(
    settings: AppSettings | None = None,
    repository: Any | None = None,
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
    app.config["REPOSITORY"] = repository or InfluxReadRepository(settings.influx)
    app.config["NODE_STALE_AFTER_SECONDS"] = settings.node_stale_after_seconds

    register_routes(app)
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
        latest_payload = _repository().latest()
        stale_after_seconds = int(current_app.config["NODE_STALE_AFTER_SECONDS"])
        return jsonify(
            latest_with_node_status(
                latest_payload,
                stale_after_seconds=stale_after_seconds,
            )
        )

    @app.get("/api/readings")
    def readings() -> Any:
        query = readings_query_from_params(request.args)
        return jsonify(_repository().readings(query))

    @app.get("/api/nodes")
    def nodes() -> Any:
        stale_after_seconds = int(current_app.config["NODE_STALE_AFTER_SECONDS"])
        return jsonify(_repository().nodes(stale_after_seconds=stale_after_seconds))


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(QueryValidationError)
    def query_validation_error(exc: QueryValidationError) -> Any:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    @app.errorhandler(HTTPException)
    def http_error(exc: HTTPException) -> Any:
        return jsonify({"error": exc.name, "message": exc.description}), exc.code

    @app.errorhandler(Exception)
    def unhandled_error(exc: Exception) -> Any:
        LOGGER.exception("API request failed: %s", exc)
        return jsonify({"error": "service_unavailable", "message": "backend query failed"}), 503


def _repository() -> Any:
    return current_app.config["REPOSITORY"]
