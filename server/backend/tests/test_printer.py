from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from app.config import (
    AirQualitySettings,
    AppSettings,
    InfluxSettings,
    MonitoringExportSettings,
    MqttSettings,
)
from app.printer_adapter import (
    HomeAssistantPrinterAdapter,
    PrinterAdapterError,
    discover_bambu_entities,
    normalize_printer_state,
    printer_state_from_home_assistant,
    unavailable_printer_state,
)
from app.printer_config import PrinterObserverSettings
from app.printer_model import (
    NormalizedPrinterState,
    PrinterState,
    PrintSession,
    ValueProvenance,
    print_session_point,
    printer_state_point,
)
from app.printer_persistence import PrinterStore
from app.printer_queries import environment_summary_response, printer_environment_flux
from app.web import create_app

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def _settings(tmp_path: Path, *fields: str) -> PrinterObserverSettings:
    entities = {
        "online": "binary_sensor.x2d_online",
        "print_status": "sensor.x2d_print_status",
    }
    entities.update({field: f"sensor.x2d_{field}" for field in fields})
    return PrinterObserverSettings(
        printer_id="x2d",
        printer_model="X2D",
        home_assistant_url="http://127.0.0.1:8123",
        entities=entities,
        database_path=tmp_path / "printer.sqlite3",
    ).validated()


def _ha_entity(
    state: object,
    *,
    attributes: dict[str, object] | None = None,
    timestamp: datetime = NOW,
) -> dict[str, object]:
    return {
        "state": state,
        "attributes": attributes or {},
        "last_reported": timestamp.isoformat(),
    }


def _ha_states(settings: PrinterObserverSettings, **values: object):
    states = {
        settings.entities["online"]: _ha_entity(values.pop("online", "on")),
        settings.entities["print_status"]: _ha_entity(
            values.pop("print_status", "idle")
        ),
    }
    for field, value in values.items():
        attributes = None
        if isinstance(value, tuple):
            value, attributes = value
        states[settings.entities[field]] = _ha_entity(value, attributes=attributes)
    return states


def _state(
    when: datetime,
    normalized: NormalizedPrinterState,
    *,
    online: bool = True,
    job_id: str | None = "job-1",
    job_name: str | None = "part.3mf",
    material: str | None = None,
) -> PrinterState:
    provenance = (
        {"active_material": ValueProvenance.OBSERVED} if material is not None else {}
    )
    return PrinterState(
        printer_id="x2d",
        printer_model="X2D",
        online=online,
        normalized_state=normalized,
        source="home_assistant",
        source_timestamp=when,
        observed_at=when,
        job_id=job_id,
        job_name=job_name,
        active_material=material,
        provenance=provenance,
    )


def test_idle_printer_normalization_keeps_missing_optional_fields_unknown(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    state = printer_state_from_home_assistant(
        _ha_states(settings), settings, observed_at=NOW
    )

    assert state.online
    assert state.normalized_state is NormalizedPrinterState.IDLE
    assert not state.session_active
    assert not state.printer_is_printing
    assert state.progress_percent is None
    assert state.nozzle_2_temperature is None
    assert state.active_material is None
    assert state.provenance == {}


def test_active_x2d_dual_nozzle_progress_layers_temperatures_and_ams(
    tmp_path: Path,
) -> None:
    fields = (
        "job_name",
        "progress_percent",
        "remaining_time",
        "current_layer",
        "total_layers",
        "nozzle_1_temperature",
        "nozzle_1_target",
        "nozzle_2_temperature",
        "nozzle_2_target",
        "bed_temperature",
        "bed_target",
        "chamber_temperature",
        "active_tool",
        "ams_state",
        "ams_slot",
    )
    settings = _settings(tmp_path, *fields)
    states = _ha_states(
        settings,
        print_status="RUNNING",
        job_name="two-color dragon.3mf",
        progress_percent="37.5",
        remaining_time=("01:30", {"unit_of_measurement": "min"}),
        current_layer="74",
        total_layers="200",
        nozzle_1_temperature=("220", {"unit_of_measurement": "°C"}),
        nozzle_1_target="225",
        nozzle_2_temperature=("482", {"unit_of_measurement": "°F"}),
        nozzle_2_target="230",
        bed_temperature="60",
        bed_target="65",
        chamber_temperature="38",
        active_tool="left",
        ams_state="ready",
        ams_slot=("A1", {"type": "PLA", "name": "Blue PLA"}),
    )

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert state.normalized_state is NormalizedPrinterState.PRINTING
    assert state.printer_is_printing
    assert state.progress_percent == 37.5
    assert state.remaining_seconds == 90 * 60
    assert (state.current_layer, state.total_layers) == (74, 200)
    assert state.nozzle_1_temperature == 220
    assert state.nozzle_2_temperature == 250
    assert state.chamber_temperature == 38
    assert state.ams_state == "ready"
    assert state.ams_slot == "A1"
    assert state.active_material == "PLA"
    assert state.active_filament == "Blue PLA"
    assert (
        state.provenance["active_material"] is ValueProvenance.INFERRED_ACTIVE_AMS_TRAY
    )


def test_direct_material_entity_is_observed_not_inferred(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "active_material", "ams_slot")
    state = printer_state_from_home_assistant(
        _ha_states(
            settings,
            print_status="running",
            active_material="PETG",
            ams_slot=("A2", {"type": "PLA"}),
        ),
        settings,
        observed_at=NOW,
    )
    assert state.active_material == "PETG"
    assert state.provenance["active_material"] is ValueProvenance.OBSERVED


def test_stale_home_assistant_entities_become_explicitly_offline(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    old = NOW - timedelta(seconds=settings.stale_after_seconds + 1)
    states = {
        settings.entities["online"]: _ha_entity("on", timestamp=old),
        settings.entities["print_status"]: _ha_entity("running", timestamp=old),
    }
    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)
    assert state.normalized_state is NormalizedPrinterState.OFFLINE
    assert not state.online
    assert state.unavailable_reason == "Home Assistant printer entities are stale"


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("idle", NormalizedPrinterState.IDLE),
        ("prepare", NormalizedPrinterState.PREPARING),
        ("running", NormalizedPrinterState.PRINTING),
        ("pause", NormalizedPrinterState.PAUSED),
        ("finish", NormalizedPrinterState.COMPLETED),
        ("failed", NormalizedPrinterState.FAILED),
        ("cancelled", NormalizedPrinterState.CANCELLED),
        ("new-firmware-value", NormalizedPrinterState.UNKNOWN),
    ),
)
def test_central_state_mapping(raw: str, expected: NormalizedPrinterState) -> None:
    assert normalize_printer_state(raw, online=True) is expected
    assert normalize_printer_state(raw, online=False) is NormalizedPrinterState.OFFLINE


def test_adapter_timeout_is_credential_safe_and_yields_explicit_unavailable_state(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)

    def timeout(*_args, **_kwargs):
        raise TimeoutError

    adapter = HomeAssistantPrinterAdapter(
        settings, "super-secret-token", opener=timeout
    )
    with pytest.raises(PrinterAdapterError) as error:
        adapter.fetch()
    assert "super-secret-token" not in str(error.value)
    unavailable = unavailable_printer_state(settings, reason=str(error.value))
    assert not unavailable.online
    assert unavailable.normalized_state is NormalizedPrinterState.OFFLINE


def test_discovery_redacts_secrets_serials_and_non_scalar_attributes() -> None:
    candidates = discover_bambu_entities(
        [
            {
                "entity_id": "sensor.x2d_serial_number",
                "state": "01P00SECRET",
                "attributes": {
                    "friendly_name": "Bambu X2D serial number",
                    "access_code": "12345678",
                    "ip_address": "192.0.2.10",
                    "material": "PLA",
                    "nested": {"secret": True},
                },
            }
        ]
    )
    assert candidates[0]["state"] == "<redacted>"
    assert candidates[0]["attributes"] == {
        "friendly_name": "Bambu X2D serial number",
        "material": "PLA",
    }
    assert "01P00SECRET" not in repr(candidates)
    assert "12345678" not in repr(candidates)


def test_session_start_pause_resume_offline_reconnect_restart_and_idempotency(
    tmp_path: Path,
) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    created = store.process(_state(NOW, NormalizedPrinterState.PREPARING))
    session_id = created[0].session_id

    assert store.process(_state(NOW, NormalizedPrinterState.PREPARING)) == ()
    store.process(_state(NOW + timedelta(minutes=1), NormalizedPrinterState.PRINTING))
    store.process(_state(NOW + timedelta(minutes=2), NormalizedPrinterState.PAUSED))
    store.process(_state(NOW + timedelta(minutes=3), NormalizedPrinterState.PRINTING))
    store.process(
        _state(
            NOW + timedelta(minutes=4),
            NormalizedPrinterState.OFFLINE,
            online=False,
        )
    )

    restarted = PrinterStore(tmp_path / "printer.sqlite3")
    assert (
        restarted.process(
            _state(NOW + timedelta(minutes=5), NormalizedPrinterState.PRINTING)
        )
        == ()
    )
    sessions = restarted.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == session_id
    assert sessions[0].active


@pytest.mark.parametrize(
    ("terminal", "expected"),
    (
        (NormalizedPrinterState.COMPLETED, "completed"),
        (NormalizedPrinterState.CANCELLED, "cancelled"),
        (NormalizedPrinterState.FAILED, "failed"),
    ),
)
def test_terminal_results_are_confirmed_and_duplicate_delivery_is_idempotent(
    tmp_path: Path,
    terminal: NormalizedPrinterState,
    expected: str,
) -> None:
    store = PrinterStore(tmp_path / f"{expected}.sqlite3")
    store.initialize()
    store.process(_state(NOW, NormalizedPrinterState.PRINTING))
    assert store.process(_state(NOW + timedelta(minutes=1), terminal)) == ()
    changed = store.process(_state(NOW + timedelta(minutes=2), terminal))
    assert changed[0].result == expected
    assert changed[0].duration_seconds == 120
    assert store.process(_state(NOW + timedelta(minutes=2), terminal)) == ()
    assert len(store.list_sessions()) == 1


def test_terminal_outcome_survives_finish_to_idle_settling(tmp_path: Path) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    store.process(_state(NOW, NormalizedPrinterState.PRINTING))
    store.process(_state(NOW + timedelta(minutes=1), NormalizedPrinterState.COMPLETED))
    closed = store.process(
        _state(NOW + timedelta(minutes=2), NormalizedPrinterState.IDLE)
    )
    assert closed[0].result == "completed"


def test_repeated_filename_creates_distinct_sessions_after_terminal_transition(
    tmp_path: Path,
) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    first = store.process(_state(NOW, NormalizedPrinterState.PRINTING, job_id=None))[0]
    store.process(
        _state(
            NOW + timedelta(minutes=1),
            NormalizedPrinterState.CANCELLED,
            job_id=None,
        )
    )
    store.process(
        _state(
            NOW + timedelta(minutes=2),
            NormalizedPrinterState.CANCELLED,
            job_id=None,
        )
    )
    second = store.process(
        _state(
            NOW + timedelta(minutes=3),
            NormalizedPrinterState.PREPARING,
            job_id=None,
        )
    )[0]
    assert first.session_id != second.session_id
    assert len(store.list_sessions()) == 2


def test_latest_finished_session_ignores_a_new_active_print(tmp_path: Path) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    first = store.process(_state(NOW, NormalizedPrinterState.PRINTING))[0]
    store.process(_state(NOW + timedelta(minutes=1), NormalizedPrinterState.COMPLETED))
    store.process(_state(NOW + timedelta(minutes=2), NormalizedPrinterState.COMPLETED))
    store.process(
        _state(
            NOW + timedelta(minutes=3),
            NormalizedPrinterState.PRINTING,
            job_id="job-2",
        )
    )
    assert store.latest_finished_session().session_id == first.session_id
    assert store.latest_session().job_id == "job-2"


def test_stable_job_change_on_reconnect_closes_old_and_opens_new(
    tmp_path: Path,
) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    first = store.process(_state(NOW, NormalizedPrinterState.PRINTING, job_id="a"))[0]
    changed = store.process(
        _state(
            NOW + timedelta(minutes=5),
            NormalizedPrinterState.PRINTING,
            job_id="b",
        )
    )
    assert len(changed) == 2
    assert changed[0].session_id == first.session_id
    assert changed[0].result == "unknown"
    assert changed[1].job_id == "b"


def test_material_change_is_not_silently_attributed_to_one_material(
    tmp_path: Path,
) -> None:
    store = PrinterStore(tmp_path / "printer.sqlite3")
    store.initialize()
    store.process(_state(NOW, NormalizedPrinterState.PRINTING, material="PLA"))
    store.process(
        _state(
            NOW + timedelta(minutes=1),
            NormalizedPrinterState.PRINTING,
            material="PETG",
        )
    )
    session = store.latest_session()
    assert session is not None
    assert session.material == "multiple"
    assert session.material_provenance is ValueProvenance.UNKNOWN


def test_influx_points_keep_unbounded_text_out_of_tags() -> None:
    state = _state(NOW, NormalizedPrinterState.PRINTING)
    state_point = printer_state_point(state, measurement="printer_state")
    assert set(state_point.tags) == {"printer_id", "printer_model", "source"}
    assert state_point.fields["job_name"] == "part.3mf"

    session = PrintSession(
        session_id="session-unique",
        printer_id="x2d",
        job_id="job-unique",
        job_name="unique file name.3mf",
        started_at=NOW,
        start_provenance=ValueProvenance.OBSERVED,
        ended_at=NOW + timedelta(minutes=10),
        end_provenance=ValueProvenance.OBSERVED,
        result="completed",
        material="PLA",
        material_provenance=ValueProvenance.OBSERVED,
        active_tool="left",
        ams_slot="A1",
        source="home_assistant",
        updated_at=NOW + timedelta(minutes=10),
    )
    session_point = print_session_point(session)
    assert set(session_point.tags) == {"printer_id", "source"}
    assert session_point.fields["session_id"] == "session-unique"
    assert session_point.fields["job_name"] == "unique file name.3mf"


def test_environment_summary_uses_configured_windows_and_observational_wording() -> (
    None
):
    end = NOW + timedelta(minutes=10)
    session = PrintSession(
        session_id="session-1",
        printer_id="x2d",
        job_id="job-1",
        job_name="part.3mf",
        started_at=NOW,
        start_provenance=ValueProvenance.OBSERVED,
        ended_at=end,
        end_provenance=ValueProvenance.OBSERVED,
        result="completed",
        material="PLA",
        material_provenance=ValueProvenance.OBSERVED,
        active_tool=None,
        ams_slot=None,
        source="home_assistant",
        updated_at=end,
    )
    points = [
        {"time": NOW - timedelta(minutes=20), "pm25": 2.0, "voc_index": 100.0},
        {"time": NOW - timedelta(minutes=10), "pm25": 4.0, "voc_index": 100.0},
        {"time": NOW, "pm25": 8.0, "voc_index": 120.0},
        {"time": NOW + timedelta(minutes=5), "pm25": 12.0, "voc_index": 140.0},
        {"time": end + timedelta(seconds=10), "voc_index": 109.0},
        {"time": end + timedelta(seconds=20), "voc_index": 108.0},
        {"time": end + timedelta(seconds=30), "voc_index": 107.0},
    ]

    result = environment_summary_response(
        session,
        points,
        baseline_minutes=30,
        recovery_minutes=120,
        location="printer_room",
    )

    assert result["observational"] is True
    assert result["metrics"]["pm25"]["print_peak"] == 12
    assert result["metrics"]["voc_index"]["change_from_baseline"] == 30
    assert result["voc_recovery_seconds"] == 10
    assert "do not establish" in result["limitations"][0]


def test_environment_flux_is_bounded_and_filters_only_location_and_known_fields() -> (
    None
):
    flux = printer_environment_flux(
        "environment_live",
        "printer_room",
        NOW - timedelta(minutes=30),
        NOW + timedelta(hours=2),
    )
    assert 'from(bucket: "environment_live")' in flux
    assert 'r.location == "printer_room"' in flux
    assert 'r._measurement == "air_quality_reading"' in flux
    assert "job_name" not in flux


def test_printer_grafana_dashboard_has_session_and_environment_foundation() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "grafana"
        / "dashboards"
        / "home-sensor-printer.json"
    )
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert titles >= {
        "X2D normalized state",
        "Print progress",
        "Printer temperatures",
        "Printer-room PM2.5, VOC, and NOx (observational)",
        "Print sessions",
    }
    assert dashboard["annotations"]["list"][0]["name"] == "Print starts and ends"
    serialized = json.dumps(dashboard)
    assert "environment_live" in serialized
    assert "print_session" in serialized


class _SensorRepository:
    def latest(self):
        return {
            "generated_at": "2026-08-11T12:00:00Z",
            "environment": [],
            "air_quality": [],
        }


class _PrinterRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def current(self):
        if self.fail:
            raise TimeoutError("printer only")
        return {"printer_id": "x2d", "normalized_state": "idle"}

    def sessions(self, *, limit: int):
        return {"sessions": [], "limit": limit}

    def environment_summary(self):
        return {"available": False, "reason": "no_print_session"}


class _Status:
    def snapshot(self):
        return {}


def _app_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        log_level="INFO",
        node_stale_after_seconds=1800,
        mqtt=MqttSettings(
            host="127.0.0.1",
            port=1883,
            keepalive_seconds=60,
            client_id="test",
            username="test",
            password="test",
            sensor_topic="home/sensors/+",
            air_topic="home/air/+",
            qos=1,
            max_payload_bytes=4096,
        ),
        influx=InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="test",
            read_token="test",
        ),
        air_quality=AirQualitySettings(),
        monitoring_exports=MonitoringExportSettings(
            database_path=tmp_path / "monitoring.sqlite3",
            output_dir=tmp_path / "exports",
        ),
    )


def _client(tmp_path: Path, printer: _PrinterRepository):
    app = create_app(
        _app_settings(tmp_path),
        repository=_SensorRepository(),
        printer_repository=printer,
        status_provider=_Status(),
    )
    return app.test_client()


def test_printer_api_is_bounded_and_read_only(tmp_path: Path) -> None:
    client = _client(tmp_path, _PrinterRepository())
    assert client.get("/api/printer").get_json()["normalized_state"] == "idle"
    assert client.get("/api/printer/sessions?limit=7").get_json()["limit"] == 7
    assert client.get("/api/printer/sessions?limit=101").status_code == 400
    assert client.post("/api/printer").status_code == 405


def test_printer_failure_does_not_break_sensor_api(tmp_path: Path) -> None:
    client = _client(tmp_path, _PrinterRepository(fail=True))
    assert client.get("/api/printer").status_code == 503
    response = client.get("/api/latest")
    assert response.status_code == 200
    assert response.get_json()["environment"] == []
