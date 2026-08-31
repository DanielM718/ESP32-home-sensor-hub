from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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
from app.printer_config import AmsObserverSettings, PrinterObserverSettings
from app.printer_intelligence import PrinterIntelligenceStore
from app.printer_model import (
    AmsUnitState,
    NormalizedPrinterState,
    PrinterState,
    PrintSession,
    ValueProvenance,
    print_session_point,
    printer_state_point,
    printer_telemetry_points,
)
from app.printer_persistence import PrinterStore
from app.printer_queries import (
    PrinterReadRepository,
    environment_summary_response,
    printer_environment_flux,
    printer_telemetry_flux,
    printer_telemetry_query_from_params,
    printer_telemetry_response,
)
from app.printer_worker import persist_observation
from app.queries import QueryValidationError
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


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    (
        ("idle", NormalizedPrinterState.IDLE),
        ("failed", NormalizedPrinterState.FAILED),
        ("running", NormalizedPrinterState.PRINTING),
    ),
)
def test_old_entity_timestamps_do_not_force_offline(
    tmp_path: Path, raw_status: str, expected: NormalizedPrinterState
) -> None:
    """A quiet printer stops changing mapped entities; that is not offline.

    Home Assistant reports these entities on change, so a settled X2D can
    legitimately exceed stale_after_seconds while remaining reachable.
    Availability comes from the mapped online entity, never from entity age.
    """

    settings = _settings(tmp_path)
    old = NOW - timedelta(seconds=settings.stale_after_seconds + 1)
    states = {
        settings.entities["online"]: _ha_entity("on", timestamp=old),
        settings.entities["print_status"]: _ha_entity(raw_status, timestamp=old),
    }

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert state.online
    assert state.normalized_state is expected
    assert state.unavailable_reason is None
    # Provenance is still reported even though the observation is old.
    assert state.source_timestamp == old


def test_old_timestamps_preserve_source_timestamp_provenance(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    older = NOW - timedelta(hours=3)
    newer = NOW - timedelta(minutes=30)
    states = {
        settings.entities["online"]: _ha_entity("on", timestamp=older),
        settings.entities["print_status"]: _ha_entity("idle", timestamp=newer),
    }

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert state.online
    assert state.normalized_state is NormalizedPrinterState.IDLE
    assert state.source_timestamp == newer  # newest mapped observation wins
    assert (
        state.observed_at - state.source_timestamp
    ).total_seconds() > settings.stale_after_seconds


def test_explicit_offline_online_entity_is_offline(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    states = {
        settings.entities["online"]: _ha_entity("off", timestamp=NOW),
        settings.entities["print_status"]: _ha_entity("running", timestamp=NOW),
    }

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert not state.online
    assert state.normalized_state is NormalizedPrinterState.OFFLINE
    assert state.unavailable_reason == "Home Assistant reports the printer offline"


@pytest.mark.parametrize("raw", ("unavailable", "unknown"))
def test_unavailable_online_entity_is_offline_with_reason(
    tmp_path: Path, raw: str
) -> None:
    settings = _settings(tmp_path)
    states = {
        settings.entities["online"]: _ha_entity(raw, timestamp=NOW),
        settings.entities["print_status"]: _ha_entity("idle", timestamp=NOW),
    }

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert not state.online
    assert state.normalized_state is NormalizedPrinterState.OFFLINE
    assert (
        state.unavailable_reason
        == "Home Assistant reports printer availability as unknown"
    )


def test_missing_online_entity_is_offline_with_reason(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    states = {settings.entities["print_status"]: _ha_entity("idle", timestamp=NOW)}

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert not state.online
    assert state.normalized_state is NormalizedPrinterState.OFFLINE
    assert (
        state.unavailable_reason == "online entity was not returned by Home Assistant"
    )


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


def test_first_class_printer_and_multiple_ams_points_are_bounded_and_typed() -> None:
    state = PrinterState(
        printer_id="x2d",
        printer_model="X2D",
        online=True,
        normalized_state=NormalizedPrinterState.PRINTING,
        source="home_assistant",
        source_timestamp=NOW,
        observed_at=NOW,
        job_id="never-a-tag",
        job_name="also never a tag.3mf",
        chamber_temperature=38.5,
        bed_temperature=60.0,
        nozzle_1_temperature=220.0,
        progress_percent=42.5,
        remaining_seconds=1234,
        ams_units=(
            AmsUnitState(
                ams_id="ams_1",
                model="AMS 2 Pro",
                active=True,
                humidity_percent=22.0,
                humidity_index=3,
                temperature=25.5,
                drying=False,
                remaining_drying_seconds=0,
            ),
            AmsUnitState(
                ams_id="ams_2",
                model="AMS 2 Pro",
                humidity_percent=41.5,
                temperature=None,
            ),
        ),
    )

    printer, ams_1, ams_2 = printer_telemetry_points(state)

    assert printer.measurement == "printer_telemetry"
    assert printer.tags == {
        "printer_id": "x2d",
        "component_type": "printer",
        "component_id": "main",
        "source": "home_assistant",
    }
    assert printer.timestamp == NOW
    assert printer.fields["chamber_temperature_c"] == 38.5
    assert printer.fields["remaining_print_seconds"] == 1234
    assert printer.fields["online"] is True
    assert "job_id" not in printer.tags and "job_name" not in printer.tags
    assert {ams_1.tags["component_id"], ams_2.tags["component_id"]} == {
        "ams_1",
        "ams_2",
    }
    assert ams_1.fields == {
        "ams_humidity": 22.0,
        "ams_temperature_c": 25.5,
        "ams_humidity_index": 3,
        "ams_remaining_drying_seconds": 0,
        "ams_active": True,
        "ams_drying": False,
    }
    assert ams_2.fields == {"ams_humidity": 41.5}
    assert "ams_temperature_c" not in ams_2.fields
    assert printer_state_point(state, measurement="printer_state").fields[
        "ams_inventory_json"
    ]


def test_adapter_normalizes_multiple_ams_telemetry_without_fabricating_values(
    tmp_path: Path,
) -> None:
    ams_units = tuple(
        AmsObserverSettings(
            ams_id=f"ams_{index}",
            model="AMS 2 Pro",
            entities={
                "active": f"binary_sensor.ams_{index}_active",
                "humidity_percent": f"sensor.ams_{index}_humidity",
                "humidity_index": f"sensor.ams_{index}_humidity_index",
                "temperature": f"sensor.ams_{index}_temperature",
                "drying": f"binary_sensor.ams_{index}_drying",
                "remaining_drying_time": f"sensor.ams_{index}_drying_remaining",
            },
        ).validated()
        for index in (1, 2)
    )
    settings = PrinterObserverSettings(
        printer_id="x2d",
        printer_model="X2D",
        home_assistant_url="http://127.0.0.1:8123",
        entities={
            "online": "binary_sensor.x2d_online",
            "print_status": "sensor.x2d_print_status",
        },
        database_path=tmp_path / "printer.sqlite3",
        ams_units=ams_units,
    ).validated()
    states = _ha_states(settings)
    states.update(
        {
            "binary_sensor.ams_1_active": _ha_entity("on"),
            "sensor.ams_1_humidity": _ha_entity("22"),
            "sensor.ams_1_humidity_index": _ha_entity("3"),
            "sensor.ams_1_temperature": _ha_entity(
                "77", attributes={"unit_of_measurement": "°F"}
            ),
            "binary_sensor.ams_1_drying": _ha_entity("off"),
            "sensor.ams_1_drying_remaining": _ha_entity("01:30"),
            "sensor.ams_2_humidity": _ha_entity("not-a-number"),
            "sensor.ams_2_temperature": _ha_entity("unavailable"),
        }
    )

    state = printer_state_from_home_assistant(states, settings, observed_at=NOW)

    assert len(state.ams_units) == 2
    first, second = state.ams_units
    assert first.humidity_percent == 22.0
    assert first.humidity_index == 3
    assert first.temperature == 25.0
    assert first.active is True and first.drying is False
    assert first.remaining_drying_seconds == 5400
    assert second.humidity_percent is None
    assert second.temperature is None
    assert second.active is None and second.drying is None


def test_worker_writes_live_and_durable_telemetry_with_failure_isolation() -> None:
    state = PrinterState(
        printer_id="x2d",
        printer_model="X2D",
        online=True,
        normalized_state=NormalizedPrinterState.IDLE,
        source="home_assistant",
        source_timestamp=NOW,
        observed_at=NOW,
        chamber_temperature=29.0,
        ams_units=(
            AmsUnitState(
                ams_id="ams_1",
                model="AMS 2 Pro",
                humidity_percent=22.0,
            ),
        ),
    )

    class Writer:
        def __init__(self) -> None:
            self.single = []
            self.many = []
            self.fail_live_telemetry = True

        def write_point_data(self, point, *, bucket=None):
            self.single.append((point.measurement, bucket))

        def write_point_data_many(self, points, *, bucket=None):
            self.many.append((tuple(points), bucket))
            if bucket == "environment_live" and self.fail_live_telemetry:
                self.fail_live_telemetry = False
                raise RuntimeError("optional telemetry failure")

    class Store:
        def __init__(self) -> None:
            self.marked = []
            self.due = True

        def permanent_sample_due(self, *_args):
            return self.due

        def mark_permanent_sample(self, printer_id, observed_at):
            self.marked.append((printer_id, observed_at))
            self.due = False

    writer = Writer()
    store = Store()
    session = PrintSession(
        session_id="session-1",
        printer_id="x2d",
        job_id="job-1",
        job_name="test.3mf",
        started_at=NOW,
        start_provenance=ValueProvenance.OBSERVED,
        ended_at=None,
        end_provenance=ValueProvenance.UNKNOWN,
        result=None,
        material=None,
        material_provenance=ValueProvenance.UNKNOWN,
        active_tool=None,
        ams_slot=None,
        source="home_assistant",
        updated_at=NOW,
    )
    persist_observation(
        writer,
        store,
        state,
        previous=state,
        changed_sessions=(session,),
        permanent_sample_seconds=300,
        live_bucket="environment_live",
    )

    assert ("printer_state", "environment_live") in writer.single
    assert ("printer_state_5m", None) in writer.single
    assert ("print_session", None) in writer.single
    assert [bucket for _points, bucket in writer.many] == ["environment_live", None]
    assert all(point.measurement == "printer_telemetry" for point in writer.many[-1][0])
    assert store.marked == [("x2d", NOW)]

    persist_observation(
        writer,
        store,
        state,
        previous=state,
        changed_sessions=(),
        permanent_sample_seconds=300,
        live_bucket="environment_live",
    )
    assert [measurement for measurement, _bucket in writer.single].count(
        "printer_state_5m"
    ) == 1
    assert [bucket for _points, bucket in writer.many].count(None) == 1


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


def test_printer_telemetry_query_is_allowlisted_and_uses_field_type_aggregation() -> (
    None
):
    query = printer_telemetry_query_from_params(
        {
            "range": "7d",
            "sensor_type": "ams",
            "printer_id": "x2d",
            "ams_id": "ams_1",
            "fields": "ams_humidity,ams_drying",
        }
    )
    flux = printer_telemetry_flux("environment", query)

    assert query.data_tier == "durable_5m_downsampled_30m"
    assert 'from(bucket: "environment")' in flux
    assert 'r._measurement == "printer_telemetry"' in flux
    assert 'r.printer_id == "x2d"' in flux
    assert 'r.component_id == "ams_1"' in flux
    assert "fn: mean" in flux
    assert "fn: last" in flux
    assert "ams_inventory_json" not in flux


@pytest.mark.parametrize(
    "params",
    [
        {"range": "30d"},
        {"sensor_type": "printer", "ams_id": "ams_1", "printer_id": "x2d"},
        {"sensor_type": "printer", "fields": "ams_humidity"},
        {"sensor_type": "ams", "fields": "chamber_temperature_c"},
        {"fields": ",,,"},
        {"printer_id": 'x2d" or true'},
    ],
)
def test_printer_telemetry_query_rejects_invalid_filters(params) -> None:
    with pytest.raises(QueryValidationError):
        printer_telemetry_query_from_params(params)


def test_printer_telemetry_response_keeps_multiple_ams_and_real_types() -> None:
    query = printer_telemetry_query_from_params(
        {"range": "24h", "fields": "ams_humidity,ams_drying"}
    )
    records = [
        SimpleNamespace(
            values={
                "_time": NOW,
                "_field": "ams_humidity",
                "_value": 22.0,
                "printer_id": "x2d",
                "component_type": "ams",
                "component_id": "ams_1",
            }
        ),
        SimpleNamespace(
            values={
                "_time": NOW,
                "_field": "ams_drying",
                "_value": False,
                "printer_id": "x2d",
                "component_type": "ams",
                "component_id": "ams_1",
            }
        ),
        SimpleNamespace(
            values={
                "_time": NOW,
                "_field": "ams_humidity",
                "_value": 41.0,
                "printer_id": "x2d",
                "component_type": "ams",
                "component_id": "ams_2",
            }
        ),
    ]

    response = printer_telemetry_response(records, query)

    assert [series["source_id"] for series in response["series"]] == [
        "x2d/ams_1",
        "x2d/ams_2",
    ]
    first = response["series"][0]
    assert first["points"] == [
        {"time": "2026-08-11T12:00:00Z", "ams_humidity": 22.0, "ams_drying": False}
    ]


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
        "AMS humidity (durable telemetry)",
        "AMS temperature (durable telemetry)",
    }
    assert dashboard["annotations"]["list"][0]["name"] == "Print starts and ends"
    serialized = json.dumps(dashboard)
    assert "environment_live" in serialized
    assert "print_session" in serialized
    assert "printer_telemetry" in serialized
    assert "ams_humidity" in serialized
    assert "ams_temperature_c" in serialized
    assert "ams_inventory_json" not in serialized


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

    def telemetry(self, query):
        return {
            "range": query.range_key,
            "window": query.window_every,
            "data_tier": query.data_tier,
            "series": [
                {
                    "sensor_type": "ams",
                    "source_id": "x2d/ams_1",
                    "printer_id": "x2d",
                    "ams_id": "ams_1",
                    "fields": ["ams_humidity"],
                    "points": [
                        {
                            "timestamp": "2026-08-11T12:00:00Z",
                            "ams_humidity": 22.0,
                        }
                    ],
                }
            ],
        }

    def sessions(self, *, limit: int):
        return {"sessions": [], "limit": limit}

    def history(self, *, limit: int):
        return {"history": [{"history_id": "one"}], "limit": limit}

    def history_item(self, history_id: str):
        return {"history_id": history_id} if history_id == "one" else None

    def usage(self):
        return {"available": True, "usage": {"tracked_print_hours": 209.14}}

    def maintenance(self):
        return {"tasks": [], "local_record_only": True, "printer_control": False}

    def maintenance_events(self, *, limit: int, pending_only: bool):
        return {
            "available": True,
            "events": [],
            "limit": limit,
            "pending": pending_only,
        }

    def complete_maintenance(self, task_id: str, *, notes: str, completed_at):
        return {
            "task_id": task_id,
            "notes": notes,
            "local_record_only": True,
            "printer_control": False,
        }

    def complete_all_maintenance(self, *, notes: str, completed_at):
        return {
            "completed_task_count": 2,
            "notes": notes,
            "local_record_only": True,
            "printer_control": False,
        }

    def environment_summary(self, history_id=None):
        return {
            "available": False,
            "reason": "no_print_session",
            "history_id": history_id,
        }


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
    telemetry = client.get(
        "/api/printer/telemetry?range=7d&sensor_type=ams"
        "&printer_id=x2d&ams_id=ams_1&fields=ams_humidity"
    )
    assert telemetry.status_code == 200
    assert telemetry.get_json()["range"] == "7d"
    assert telemetry.get_json()["series"][0]["source_id"] == "x2d/ams_1"
    assert client.get("/api/printer/telemetry?range=30d").status_code == 400
    assert (
        client.get("/api/printer/telemetry?fields=ams_inventory_json").status_code
        == 400
    )
    assert client.get("/api/printer/telemetry?fields=,,,").status_code == 400
    assert client.post("/api/printer/telemetry").status_code == 405
    assert client.get("/api/printer/history?limit=500").status_code == 200
    assert client.get("/api/printer/history?limit=501").status_code == 400
    assert client.get("/api/printer/sessions/one").status_code == 200
    assert client.get("/api/printer/sessions/missing").status_code == 404
    assert (
        client.get("/api/printer/environment-summary?session_id=one").get_json()[
            "history_id"
        ]
        == "one"
    )


def test_maintenance_completion_is_explicit_and_local_only(tmp_path: Path) -> None:
    client = _client(tmp_path, _PrinterRepository())
    route = "/api/printer/maintenance/user_inspection/complete"
    assert client.post(route, json={}).status_code == 400
    response = client.post(route, json={"confirm": True, "notes": "done"})
    assert response.status_code == 201
    assert response.get_json() == {
        "task_id": "user_inspection",
        "notes": "done",
        "local_record_only": True,
        "printer_control": False,
    }
    for forbidden in ("start", "pause", "resume", "cancel", "command"):
        assert client.post(f"/api/printer/{forbidden}").status_code in {404, 405}


def test_usage_and_maintenance_event_routes_are_bounded_and_read_only(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path, _PrinterRepository())

    usage = client.get("/api/printer/usage")
    events = client.get("/api/printer/maintenance/events?limit=25&pending=true")

    assert usage.status_code == 200
    assert usage.get_json()["usage"]["tracked_print_hours"] == 209.14
    assert events.get_json() == {
        "available": True,
        "events": [],
        "limit": 25,
        "pending": True,
    }
    assert client.get("/api/printer/maintenance/events?limit=0").status_code == 400
    assert client.get("/api/printer/maintenance/events?limit=501").status_code == 400
    assert (
        client.get("/api/printer/maintenance/events?pending=maybe").status_code == 400
    )
    assert client.post("/api/printer/usage").status_code == 405


def test_mark_all_maintenance_requires_explicit_confirmation(tmp_path: Path) -> None:
    client = _client(tmp_path, _PrinterRepository())
    route = "/api/printer/maintenance/complete-all"

    assert client.post(route, json={}).status_code == 400
    assert client.post(route, json={"confirm": "yes"}).status_code == 400
    assert client.post(route, json={"confirm": True, "notes": 5}).status_code == 400
    response = client.post(route, json={"confirm": True, "notes": "baseline"})

    assert response.status_code == 201
    assert response.get_json() == {
        "completed_task_count": 2,
        "notes": "baseline",
        "local_record_only": True,
        "printer_control": False,
    }


def test_printer_api_exposes_tracked_runtime_and_keeps_prior_usage_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "printer.sqlite3"
    local = PrinterStore(database)
    local.initialize()
    intelligence = PrinterIntelligenceStore(database)
    intelligence.initialize()
    intelligence.sync_maintenance_tasks(
        (), now=datetime(2026, 8, 15, tzinfo=timezone.utc)
    )
    start = datetime(2026, 8, 14, 8, tzinfo=timezone.utc)
    local.process(
        _printing_state(start, NormalizedPrinterState.PRINTING, job_id="tracked")
    )
    local.process(
        _printing_state(
            start + timedelta(hours=3),
            NormalizedPrinterState.COMPLETED,
            job_id="tracked",
        )
    )
    local.process(
        _printing_state(
            start + timedelta(hours=3, seconds=15),
            NormalizedPrinterState.COMPLETED,
            job_id="tracked",
        )
    )
    repository = PrinterReadRepository(
        InfluxSettings(
            url="http://127.0.0.1:8086",
            org="test",
            bucket="environment",
            write_token="test",
            read_token="test",
        ),
        database_path=database,
    )
    client = _client(tmp_path, repository)

    usage = client.get("/api/printer/usage").get_json()["usage"]
    maintenance = client.get("/api/printer/maintenance").get_json()

    assert usage["tracked_print_hours"] == 3.0042
    assert usage["tracked_job_count"] == 1
    assert usage["tracked_history_complete"] is False
    assert usage["locally_observed_print_hours"] == 3.0042
    assert usage["maintenance_effective_provenance"] == "locally_observed"
    assert maintenance["summary"]["overall_state"] == "baseline_required"
    assert maintenance["tasks"][0]["printer_control"] is False
    assert maintenance["local_record_only"] is True

    completion = client.post(
        "/api/printer/maintenance/x2d_live_view_camera_cleaning/complete",
        json={"confirm": True, "notes": "cleaned"},
    )
    assert completion.status_code == 201
    assert completion.get_json()["printer_control"] is False


def _printing_state(
    when: datetime, normalized: NormalizedPrinterState, *, job_id: str
) -> PrinterState:
    return PrinterState(
        printer_id="x2d",
        printer_model="X2D",
        online=True,
        normalized_state=normalized,
        source="home_assistant",
        source_timestamp=when,
        observed_at=when,
        job_id=job_id,
        job_name="tracked job",
    )


def test_printer_failure_does_not_break_sensor_api(tmp_path: Path) -> None:
    client = _client(tmp_path, _PrinterRepository(fail=True))
    assert client.get("/api/printer").status_code == 503
    response = client.get("/api/latest")
    assert response.status_code == 200
    assert response.get_json()["environment"] == []
