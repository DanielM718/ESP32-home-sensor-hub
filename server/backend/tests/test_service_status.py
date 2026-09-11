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


# --- uptime ---------------------------------------------------------------

REAL_SYSTEMD_OUTPUT = (
    "Id=present.service\n"
    "Description=Present test service\n"
    "LoadState=loaded\n"
    "ActiveState=active\n"
    "SubState=running\n"
    "ActiveEnterTimestamp=Mon 2026-08-31 11:51:20 EDT\n"
    "ActiveEnterTimestampMonotonic=431138553910\n"
)


def _provider_over(stdout: str, *, monotonic: float = 466808.443):
    def runner(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=stdout)

    return SystemStatusProvider(
        runner=runner,
        services=(("Present", "present.service", True),),
        monotonic=lambda: monotonic,
    )


def test_uptime_is_derived_from_the_property_systemd_actually_emits() -> None:
    """Regression: uptime_seconds was null for every unit in production.

    SYSTEMD_PROPERTIES asked for ActiveEnterTimestampUSec, which is not a
    systemd property. systemd silently omits an unknown property rather than
    failing, so the parse always saw None. The previous test supplied a
    hand-written USec line that real systemd never sends, so the gap survived.
    This output is exactly what `systemctl show` returns on the deployment.
    """

    snapshot = _provider_over(REAL_SYSTEMD_OUTPUT).snapshot()

    # 466808.443s since boot minus 431138.553910s at activation.
    assert snapshot["services"][0]["uptime_seconds"] == 35669


def test_the_monotonic_property_is_requested_from_systemd() -> None:
    requested: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        requested.append(command)
        return SimpleNamespace(returncode=0, stdout=REAL_SYSTEMD_OUTPUT)

    SystemStatusProvider(
        runner=runner, services=(("Present", "present.service", True),)
    ).snapshot()

    assert "ActiveEnterTimestampMonotonic" in requested[0][-1]


def test_a_unit_that_never_activated_reports_no_uptime() -> None:
    """systemd reports 0 rather than omitting the stamp for an inactive unit."""

    snapshot = _provider_over(
        "Id=idle.service\nLoadState=loaded\nActiveState=inactive\n"
        "SubState=dead\nActiveEnterTimestamp=\n"
        "ActiveEnterTimestampMonotonic=0\n"
    ).snapshot()

    assert snapshot["services"][0]["uptime_seconds"] is None


def test_uptime_uses_the_monotonic_clock_so_an_ntp_step_cannot_invent_it() -> None:
    """This Pi has no RTC, so wall time can jump hours once NTP settles."""

    early = _provider_over(REAL_SYSTEMD_OUTPUT, monotonic=466808.443).snapshot()
    later = _provider_over(REAL_SYSTEMD_OUTPUT, monotonic=466818.443).snapshot()

    assert later["services"][0]["uptime_seconds"] - early["services"][0][
        "uptime_seconds"
    ] == 10


def test_a_systemd_that_supplies_usec_still_works() -> None:
    """The older property is still honoured when a systemd actually sends it."""

    snapshot = _provider_over(
        "Id=present.service\nLoadState=loaded\nActiveState=active\n"
        "SubState=running\nActiveEnterTimestampUSec=1786360800000000\n"
    ).snapshot()

    assert snapshot["services"][0]["uptime_seconds"] is not None


def test_the_printer_observer_is_a_reported_core_unit() -> None:
    """It runs in production but was absent from the allow-list."""

    from app.service_status import SERVICE_DEFINITIONS

    units = {unit: core for _name, unit, core in SERVICE_DEFINITIONS}

    assert units["home-sensor-printer-observer.service"] is True
    # Neighbouring Butters units are observed but must never be core.
    assert units["butters-web.service"] is False
    assert units["butters-action-broker.socket"] is False
