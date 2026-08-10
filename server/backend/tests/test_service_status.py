from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from app.service_status import SystemStatusProvider
from app.web import create_app
from test_web import ConcurrentRepository, settings


def test_system_status_reports_active_and_missing_services_independently() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[2] == "present.service":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Id=present.service\nLoadState=loaded\nActiveState=active\n"
                    "SubState=running\nDescription=Present test service\n"
                    "ActiveEnterTimestamp=Mon 2026-08-10 10:00:00 EDT\n"
                    "ActiveEnterTimestampUSec=1786360800000000\n"
                ),
            )
        return SimpleNamespace(
            returncode=4,
            stdout=(
                "Id=missing.service\nLoadState=not-found\nActiveState=inactive\n"
                "SubState=dead\nDescription=missing.service\n"
            ),
        )

    provider = SystemStatusProvider(
        runner=runner,
        services=(
            ("Present", "present.service", True),
            ("Missing", "missing.service", False),
        ),
    )
    snapshot = provider.snapshot()

    assert [call[2] for call in calls] == ["present.service", "missing.service"]
    assert snapshot["services"][0]["installed"] is True
    assert snapshot["services"][0]["active"] is True
    assert snapshot["services"][1]["installed"] is False
    assert snapshot["services"][1]["active"] is False
    assert snapshot["services"][1]["commands"]["logs"] == (
        "journalctl -u missing.service -n 100 --no-pager"
    )


def test_status_endpoint_includes_safe_runtime_configuration(tmp_path) -> None:
    class FakeProvider:
        def snapshot(self) -> dict[str, object]:
            return {
                "checked_at_utc": "2026-08-10T12:00:00Z",
                "hostname": "sensor-pi",
                "backend": {"status": "ok"},
                "services": [],
            }

    app_settings = settings()
    # Keep workflow state in the test's writable temporary directory.
    app_settings = replace(
        app_settings,
        monitoring_exports=replace(
            app_settings.monitoring_exports,
            database_path=tmp_path / "monitoring.sqlite3",
            output_dir=tmp_path / "exports",
        ),
    )
    client = create_app(
        app_settings,
        repository=ConcurrentRepository(),
        status_provider=FakeProvider(),
    ).test_client()

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.json["hostname"] == "sensor-pi"
    assert response.json["configuration"] == {
        "node_stale_after_seconds": 1800,
        "air_quality_stale_after_seconds": 20,
        "sen66_expected_publish_seconds": 5,
        "raw_retention_seconds": 259200,
        "stored_air_quality_resolution_seconds": 900,
    }
