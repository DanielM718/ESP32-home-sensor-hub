"""Flask routes for monitoring sessions and persistent CSV export jobs."""

from __future__ import annotations

from typing import Any

from flask import Flask, current_app, jsonify, request, send_file

from app.workflow_services import ExportService, MonitoringService
from app.workflows import (
    AIR_QUALITY_FIELDS,
    CSV_FORMATS,
    ENVIRONMENT_FIELDS,
    FIELD_DISPLAY_UNITS,
    FIELD_GROUPS,
    FIELD_LABELS,
    FIELD_UNITS,
    MIN_MONITORING_SECONDS,
    RESOLUTION_OPTIONS,
    RESOLUTIONS,
    SUPPORTED_FIELDS,
)


def register_workflow_routes(app: Flask) -> None:
    @app.get("/api/workflows/options")
    def workflow_options() -> Any:
        return jsonify(
            {
                "server_time_utc": _export_service().serialize_time(),
                "raw_retention_seconds": current_app.config[
                    "MONITORING_MAX_DURATION_SECONDS"
                ],
                "minimum_monitoring_seconds": MIN_MONITORING_SECONDS,
                "resolutions": list(RESOLUTIONS),
                "monitoring_resolutions": list(RESOLUTION_OPTIONS),
                "export_resolutions": [
                    {
                        **option,
                        "data_source": (
                            "stored_air_quality_aggregate"
                            if option["value"] == "15m"
                            else "retained_raw"
                            if option["value"] != "raw"
                            else "raw"
                        ),
                    }
                    for option in RESOLUTION_OPTIONS
                ],
                "csv_formats": list(CSV_FORMATS),
                "fields": [
                    {
                        "name": field,
                        "label": FIELD_LABELS[field],
                        "unit": FIELD_UNITS[field],
                        "display_unit": FIELD_DISPLAY_UNITS[field],
                        "group": FIELD_GROUPS[field],
                        "sensor_types": [
                            sensor_type
                            for sensor_type, allowed in (
                                ("environment", ENVIRONMENT_FIELDS),
                                ("air_quality", AIR_QUALITY_FIELDS),
                            )
                            if field in allowed
                        ],
                    }
                    for field in SUPPORTED_FIELDS
                ],
            }
        )

    @app.post("/api/monitoring/sessions")
    def create_monitoring_session() -> Any:
        session = _monitoring_service().create(request.get_json(silent=True))
        return jsonify(session), 201

    @app.get("/api/monitoring/sessions")
    def list_monitoring_sessions() -> Any:
        service = _monitoring_service()
        sessions = service.list()
        return jsonify(
            {
                "server_time_utc": service.server_time(),
                "sessions": sessions,
            }
        )

    @app.get("/api/monitoring/sessions/<uuid:session_id>")
    def get_monitoring_session(session_id: Any) -> Any:
        return jsonify(_monitoring_service().get(str(session_id)))

    @app.post("/api/monitoring/sessions/<uuid:session_id>/stop")
    def stop_monitoring_session(session_id: Any) -> Any:
        return jsonify(_monitoring_service().stop(str(session_id)))

    @app.delete("/api/monitoring/sessions/<uuid:session_id>")
    def delete_monitoring_session(session_id: Any) -> Any:
        _monitoring_service().delete(str(session_id))
        return "", 204

    @app.get("/api/monitoring/sessions/<uuid:session_id>/preview")
    def preview_monitoring_session(session_id: Any) -> Any:
        return jsonify(_monitoring_service().preview(str(session_id)))

    @app.post("/api/exports")
    def create_export() -> Any:
        job = _export_service().create(request.get_json(silent=True))
        return jsonify(job), 202

    @app.get("/api/exports")
    def list_exports() -> Any:
        service = _export_service()
        jobs = service.list()
        return jsonify({"server_time_utc": service.serialize_time(), "exports": jobs})

    @app.get("/api/exports/<uuid:export_id>")
    def get_export(export_id: Any) -> Any:
        return jsonify(_export_service().get(str(export_id)))

    @app.post("/api/exports/<uuid:export_id>/cancel")
    def cancel_export(export_id: Any) -> Any:
        return jsonify(_export_service().cancel(str(export_id)))

    @app.delete("/api/exports/<uuid:export_id>")
    def delete_export(export_id: Any) -> Any:
        _export_service().delete(str(export_id))
        return "", 204

    @app.get("/api/exports/<uuid:export_id>/download")
    def download_export(export_id: Any) -> Any:
        path, filename = _export_service().download(str(export_id))
        return send_file(
            path,
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=filename,
            conditional=True,
            etag=True,
            max_age=0,
        )


def _monitoring_service() -> MonitoringService:
    return current_app.config["MONITORING_SERVICE"]


def _export_service() -> ExportService:
    return current_app.config["EXPORT_SERVICE"]
