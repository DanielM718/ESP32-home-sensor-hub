from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.system_health import (
    DEGRADED,
    HEALTHY,
    UNAVAILABLE,
    UNKNOWN,
    DependencyDefinition,
    SystemHealthProvider,
    read_source_revision,
)
from app.web import create_app
from test_web import ConcurrentRepository, settings

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


class _StatusProvider:
    """Stand-in for SystemStatusProvider with a caller-chosen unit table."""

    def __init__(self, units: dict[str, dict[str, object]]) -> None:
        self.units = units

    def snapshot(self) -> dict[str, object]:
        return {
            "services": [
                {
                    "unit": unit,
                    "installed": values.get("installed", True),
                    "active": values.get("active", True),
                    "active_state": values.get("active_state", "active"),
                    "sub_state": values.get("sub_state", "running"),
                    "uptime_seconds": values.get("uptime_seconds", 10),
                }
                for unit, values in self.units.items()
            ]
        }


ALL_UNITS = {
    "home-sensor-dashboard.service": {},
    "mosquitto.service": {},
    "home-sensor-bridge.service": {},
    "influxdb.service": {},
    "home-sensor-printer-observer.service": {},
    "home-sensor-export-worker.service": {},
    "grafana-server.service": {},
    "butters-web.service": {},
    "butters-action-broker.socket": {},
}


def _latest(*, environment_ages=(5.0,), air_quality_ages=(1.0,)):
    return lambda: {
        "environment": [
            {"id": f"env{index}", "last_seen": _iso(age)}
            for index, age in enumerate(environment_ages)
        ],
        "air_quality": [
            {"id": f"aq{index}", "last_seen": _iso(age)}
            for index, age in enumerate(air_quality_ages)
        ],
    }


def _printer(*, age=10.0, online=True):
    return lambda: {
        "available": online,
        "status": "printing",
        "online": online,
        "observed_at": _iso(age),
        "source": "home_assistant",
    }


def _provider(**overrides):
    kwargs = {
        "status_provider": _StatusProvider(dict(ALL_UNITS)),
        "latest_resolver": _latest(),
        "printer_resolver": _printer(),
        "node_stale_after_seconds": 1800,
        "air_quality_stale_after_seconds": 20,
        "printer_stale_after_seconds": 300,
        "clock": _clock,
        "revision_reader": lambda: ("abcdef1234", "release_file"),
    }
    kwargs.update(overrides)
    return SystemHealthProvider(**kwargs)


def _by_id(snapshot):
    return {item["dependency_id"]: item for item in snapshot["dependencies"]}


# --- the four states ------------------------------------------------------


def test_everything_working_is_healthy() -> None:
    snapshot = _provider().snapshot()

    assert snapshot["overall_state"] == HEALTHY
    assert snapshot["counts"][UNAVAILABLE] == 0
    assert all(item["state"] == HEALTHY for item in snapshot["dependencies"])


def test_a_stale_sensor_degrades_ingest_without_claiming_an_outage() -> None:
    """The exact production case: a unit is active but a node stopped reporting.

    /api/status calls this healthy because every unit is active. The whole point
    of the health model is that it must not.
    """

    snapshot = _provider(
        latest_resolver=_latest(environment_ages=(5.0, 999_999.0))
    ).snapshot()
    dependencies = _by_id(snapshot)

    assert dependencies["sensor_ingest"]["state"] == DEGRADED
    assert dependencies["mqtt_broker"]["state"] == DEGRADED
    assert snapshot["overall_state"] == DEGRADED
    data_check = next(
        check
        for check in dependencies["sensor_ingest"]["checks"]
        if check["name"] == "data"
    )
    assert data_check["detail"]["fresh_device_count"] == 2
    assert data_check["detail"]["device_count"] == 3
    # The process signal must still read healthy, so the report distinguishes
    # "the service died" from "the service is fine but data stopped arriving".
    process_check = next(
        check
        for check in dependencies["sensor_ingest"]["checks"]
        if check["name"] == "process"
    )
    assert process_check["state"] == HEALTHY


def test_a_failed_unit_is_unavailable() -> None:
    units = dict(ALL_UNITS)
    units["influxdb.service"] = {"active": False, "active_state": "failed"}

    snapshot = _provider(status_provider=_StatusProvider(units)).snapshot()

    assert _by_id(snapshot)["influx"]["state"] == UNAVAILABLE
    assert snapshot["overall_state"] == UNAVAILABLE


def test_every_sensor_stale_is_unavailable_not_merely_degraded() -> None:
    snapshot = _provider(
        latest_resolver=_latest(environment_ages=(999_999.0,), air_quality_ages=())
    ).snapshot()

    assert _by_id(snapshot)["sensor_ingest"]["state"] == UNAVAILABLE


def test_an_uninstalled_unit_is_unknown_not_unavailable() -> None:
    """Absent is not broken. Reporting it as an outage would be a false alarm."""

    units = dict(ALL_UNITS)
    units["grafana-server.service"] = {"installed": False, "active": False}

    snapshot = _provider(status_provider=_StatusProvider(units)).snapshot()

    assert _by_id(snapshot)["grafana"]["state"] == UNKNOWN


def test_an_unconfigured_printer_observer_is_unknown() -> None:
    snapshot = _provider(
        printer_resolver=lambda: {
            "available": False,
            "status": "not_configured",
            "reason": "printer observer has not produced state",
        }
    ).snapshot()
    dependencies = _by_id(snapshot)

    assert dependencies["printer_telemetry"]["state"] == UNKNOWN
    assert dependencies["home_assistant"]["state"] == UNKNOWN


def test_stale_printer_telemetry_marks_home_assistant_unavailable() -> None:
    """Home Assistant is the only route printer telemetry takes."""

    snapshot = _provider(printer_resolver=_printer(age=100_000.0)).snapshot()
    dependencies = _by_id(snapshot)

    assert dependencies["printer_telemetry"]["state"] == UNAVAILABLE
    assert dependencies["home_assistant"]["state"] == UNAVAILABLE


def test_an_offline_printer_is_degraded_rather_than_unavailable() -> None:
    """Fresh telemetry saying "offline" means the pipeline works fine."""

    snapshot = _provider(printer_resolver=_printer(online=False)).snapshot()

    assert _by_id(snapshot)["printer_telemetry"]["state"] == DEGRADED


# --- honesty about what was actually checked ------------------------------


def test_process_only_dependencies_declare_their_weaker_basis() -> None:
    snapshot = _by_id(_provider().snapshot())

    assert snapshot["export_worker"]["basis"] == "process_only"
    assert snapshot["butters_action_broker"]["basis"] == "process_only"
    assert snapshot["sensor_ingest"]["basis"] == "process_and_data"
    assert snapshot["home_assistant"]["basis"] == "data_only"


def test_only_core_dependencies_decide_the_overall_state() -> None:
    """A Butters outage must not make the sensor dashboard look broken."""

    units = dict(ALL_UNITS)
    units["butters-web.service"] = {"active": False, "active_state": "failed"}

    snapshot = _provider(status_provider=_StatusProvider(units)).snapshot()

    assert _by_id(snapshot)["butters_web"]["state"] == UNAVAILABLE
    assert snapshot["overall_state"] == HEALTHY


def test_a_read_only_snapshot_never_names_a_unit_the_caller_chose() -> None:
    """There is no unit/host/path parameter anywhere in the public surface."""

    snapshot = _provider().snapshot()
    reported = {item["unit"] for item in snapshot["dependencies"] if item["unit"]}
    allowed = {
        definition.unit
        for definition in SystemHealthProvider(
            status_provider=_StatusProvider({})
        ).dependencies
        if definition.unit
    }

    assert reported <= allowed


# --- bounded probes -------------------------------------------------------


def test_a_hanging_collector_cannot_hang_the_snapshot() -> None:
    """The response must be bounded in wall time, not merely labelled bounded.

    A pool closed by its context manager joins its workers on exit, which passes
    every state assertion below while still taking as long as the slowest
    dependency. The elapsed-time assertion is what actually pins the bound.
    """

    import threading
    import time as time_module

    release = threading.Event()

    def hanging_resolver():
        release.wait(30)
        return {}

    provider = _provider(
        latest_resolver=hanging_resolver,
        collector_timeout_seconds=0.05,
        total_budget_seconds=0.5,
    )
    started = time_module.monotonic()
    try:
        snapshot = provider.snapshot()
        elapsed = time_module.monotonic() - started
    finally:
        release.set()

    assert elapsed < 5.0, f"snapshot waited {elapsed:.1f}s on a hung collector"
    assert "sensor_freshness" in snapshot["probe"]["timed_out"]
    assert _by_id(snapshot)["sensor_ingest"]["state"] == UNKNOWN
    # A read that never completed is evidence against InfluxDB, not for it.
    assert _by_id(snapshot)["influx"]["state"] == UNAVAILABLE


def test_a_wedged_collector_does_not_grow_a_thread_per_request() -> None:
    """A permanently stuck dependency must not leak a worker on every poll."""

    import threading

    release = threading.Event()
    starts = []

    def hanging_resolver():
        starts.append(1)
        release.wait(30)
        return {}

    provider = _provider(
        latest_resolver=hanging_resolver,
        collector_timeout_seconds=0.05,
        total_budget_seconds=0.5,
    )
    try:
        for _ in range(5):
            snapshot = provider.snapshot()
            assert "sensor_freshness" in snapshot["probe"]["timed_out"]
    finally:
        release.set()

    assert len(starts) == 1


def test_a_raising_collector_becomes_unknown_rather_than_an_error() -> None:
    def broken_resolver():
        raise RuntimeError("influx token 'super-secret' was rejected by https://host")

    snapshot = _provider(latest_resolver=broken_resolver).snapshot()
    serialized = repr(snapshot)

    assert _by_id(snapshot)["sensor_ingest"]["state"] == UNKNOWN
    assert "super-secret" not in serialized
    assert "https://host" not in serialized


def test_a_raising_status_provider_leaves_process_signals_unknown() -> None:
    class Broken:
        def snapshot(self) -> dict[str, object]:
            raise OSError("systemctl is missing at /usr/bin/systemctl")

    snapshot = _provider(status_provider=Broken()).snapshot()

    assert all(
        item["state"] == UNKNOWN
        for item in snapshot["dependencies"]
        if item["unit"] is not None
    )
    assert "/usr/bin/systemctl" not in repr(snapshot)


def test_the_probe_reports_its_own_budget_and_elapsed_time() -> None:
    snapshot = _provider().snapshot()

    assert snapshot["probe"]["total_budget_seconds"] == 6.0
    assert snapshot["probe"]["collector_timeout_seconds"] == 3.0
    assert isinstance(snapshot["probe"]["elapsed_ms"], int)


# --- no leakage -----------------------------------------------------------


def test_upstream_strings_are_bounded_before_they_reach_the_response() -> None:
    """A hostile or accidental upstream value must not become response text."""

    snapshot = _provider(
        printer_resolver=lambda: {
            "available": True,
            "online": True,
            "observed_at": _iso(1.0),
            "source": "/etc/home-sensor/printer.env leaked " + "x" * 200,
            "unavailable_reason": "Bearer abcdefghijklmnop",
        }
    ).snapshot()
    data_check = next(
        check
        for check in _by_id(snapshot)["printer_telemetry"]["checks"]
        if check["name"] == "data"
    )

    assert data_check["detail"]["source"] is None
    assert data_check["detail"]["unavailable_reason"] is None
    assert "printer.env" not in repr(snapshot)
    assert "Bearer" not in repr(snapshot)


def test_the_snapshot_carries_no_url_or_filesystem_path() -> None:
    serialized = repr(_provider().snapshot())

    assert "://" not in serialized
    assert "/etc/" not in serialized
    assert "/var/lib/" not in serialized
    assert "token" not in serialized.lower()


# --- release metadata -----------------------------------------------------


def test_the_deployed_revision_comes_from_the_release_stamp(tmp_path) -> None:
    stamp = tmp_path / "RELEASE"
    stamp.write_text("0c1d78bccd96\n", encoding="utf-8")

    assert read_source_revision(release_file=stamp, source_root=tmp_path) == (
        "0c1d78bccd96",
        "release_file",
    )


def test_a_corrupt_release_stamp_is_reported_unknown_rather_than_echoed(
    tmp_path,
) -> None:
    """A half-written or tampered stamp must not put arbitrary text in the API."""

    stamp = tmp_path / "RELEASE"
    stamp.write_text("<script>alert(1)</script>\n", encoding="utf-8")

    assert read_source_revision(release_file=stamp, source_root=tmp_path) == (
        "unknown",
        "unavailable",
    )


def test_a_deployment_without_git_or_a_stamp_reports_unknown(tmp_path) -> None:
    assert read_source_revision(
        release_file=tmp_path / "absent", source_root=tmp_path
    ) == ("unknown", "unavailable")


def test_a_failing_git_lookup_reports_unknown(tmp_path) -> None:
    (tmp_path / ".git").mkdir()

    def runner(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=128, stdout="")

    assert read_source_revision(
        release_file=tmp_path / "absent", source_root=tmp_path, runner=runner
    ) == ("unknown", "unavailable")


def test_a_broken_revision_reader_does_not_break_the_snapshot() -> None:
    def reader() -> tuple[str, str]:
        raise OSError("no such file: /opt/home-sensor/server/RELEASE")

    snapshot = _provider(revision_reader=reader).snapshot()

    assert snapshot["service"]["source_revision"] == "unknown"
    assert "/opt/home-sensor" not in repr(snapshot)


def test_the_service_block_reports_process_uptime() -> None:
    ticks = iter([1000.0, 1000.0, 1000.0, 1000.0, 1000.0])
    provider = _provider(
        monotonic=lambda: next(ticks, 1000.0), process_started_monotonic=100.0
    )

    assert provider.snapshot()["service"]["process_uptime_seconds"] == 900


# --- endpoint -------------------------------------------------------------


def _client(tmp_path, provider):
    app_settings = replace(
        settings(),
        monitoring_exports=replace(
            settings().monitoring_exports,
            database_path=tmp_path / "monitoring.sqlite3",
            output_dir=tmp_path / "exports",
        ),
    )
    return create_app(
        app_settings,
        repository=ConcurrentRepository(),
        health_provider=provider,
    ).test_client()


def test_the_endpoint_returns_the_snapshot(tmp_path) -> None:
    response = _client(tmp_path, _provider()).get("/api/system-status")

    assert response.status_code == 200
    assert response.json["overall_state"] == HEALTHY
    assert {item["dependency_id"] for item in response.json["dependencies"]} >= {
        "sensor_ingest",
        "influx",
        "printer_telemetry",
        "home_assistant",
        "butters_action_broker",
    }


def test_the_endpoint_still_answers_when_every_dependency_is_down(tmp_path) -> None:
    """Reporting an outage is this endpoint's job; it must not become one."""

    units = {unit: {"active": False, "active_state": "failed"} for unit in ALL_UNITS}
    provider = _provider(
        status_provider=_StatusProvider(units),
        latest_resolver=_latest(environment_ages=(999_999.0,), air_quality_ages=()),
        printer_resolver=_printer(age=999_999.0),
    )

    response = _client(tmp_path, provider).get("/api/system-status")

    assert response.status_code == 200
    assert response.json["overall_state"] == UNAVAILABLE


def test_a_dependency_definition_needs_at_least_one_observable_signal() -> None:
    """A dependency with nothing to check must say so, not imply health."""

    provider = _provider(
        dependencies=(DependencyDefinition("mystery", "Mystery", core=True),)
    )
    snapshot = provider.snapshot()

    assert snapshot["dependencies"][0]["state"] == UNKNOWN
    assert snapshot["dependencies"][0]["basis"] == "none"
    assert snapshot["overall_state"] == UNKNOWN
