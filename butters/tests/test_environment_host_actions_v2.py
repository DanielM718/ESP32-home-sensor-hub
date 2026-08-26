from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest
from butters.actions.broker import BrokerOperation
from butters.actions.store import ActionStateStore
from butters.assistant_config import ActionSettings, BrokerSettings, KnownDeviceSettings
from butters.integrations.actions import EnvironmentControlAdapter, HostStatusAdapter
from butters.integrations.model import IntegrationError, SensorRecord, SensorSnapshot


class FixedActions:
    def __init__(self) -> None:
        self.operations = []

    def execute(self, operation, *, cancel_event=None):
        self.operations.append(operation)
        return {"accepted": True}


class Sensors:
    def __init__(self, timestamp: str) -> None:
        self.timestamp = timestamp

    def snapshot(self):
        return SensorSnapshot(
            self.timestamp,
            (
                SensorRecord(
                    "environment",
                    "safety-sensor",
                    self.timestamp,
                    1,
                    "online",
                    {"temperature": 22.0},
                ),
            ),
        )


def _settings(**heater):
    base = KnownDeviceSettings(
        enabled=True,
        configured=True,
        maximum_duration_minutes=1,
        local_console_allowed=True,
        require_fresh_sensor=True,
        safety_entity="safety-sensor",
        safety_max_age_seconds=120,
    )
    return ActionSettings(heater=replace(base, **heater))


def test_environment_unconfigured_and_stale_safety_fail_closed(tmp_path) -> None:
    state = ActionStateStore(tmp_path / "actions.sqlite3", ActionSettings())
    actions = FixedActions()
    unavailable = EnvironmentControlAdapter(
        ActionSettings(), actions, state, Sensors("2026-08-15T12:00:00Z")
    )
    with pytest.raises(IntegrationError) as denied:
        unavailable.set("heater", "on", None, cancel_event=None)
    assert denied.value.code == "capability_unavailable"
    stale = EnvironmentControlAdapter(
        _settings(), actions, state, Sensors("2020-01-01T00:00:00Z")
    )
    with pytest.raises(IntegrationError) as blocked:
        stale.set("heater", "on", None, cancel_event=None)
    assert blocked.value.code == "safety_sensor_stale"
    assert actions.operations == []


def test_timed_environment_override_releases_on_cancellation(tmp_path) -> None:
    state = ActionStateStore(tmp_path / "actions.sqlite3", _settings())
    actions = FixedActions()
    current = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = EnvironmentControlAdapter(
        _settings(), actions, state, Sensors(current)
    )
    cancel = threading.Event()
    outcome: list[Exception] = []

    def run() -> None:
        try:
            environment.set("heater", "on", 1, cancel_event=cancel, job_id="job-one")
        except Exception as exc:  # noqa: BLE001 - captured fixture outcome
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.time() + 2
    while time.time() < deadline and not state.overrides():
        time.sleep(0.01)
    assert state.overrides()[0]["job_id"] == "job-one"
    cancel.set()
    worker.join(2)
    assert outcome and isinstance(outcome[0], IntegrationError)
    assert outcome[0].code == "cancelled"
    assert [item.value for item in actions.operations] == [
        "environment.heater_on",
        "environment.heater_off",
    ]
    assert state.overrides() == ()


def test_environment_duration_and_missing_safety_configuration_are_bounded(
    tmp_path,
) -> None:
    no_sensor = _settings(safety_entity="")
    state = ActionStateStore(tmp_path / "actions.sqlite3", no_sensor)
    actions = FixedActions()
    environment = EnvironmentControlAdapter(
        no_sensor, actions, state, Sensors("2026-08-15T12:00:00Z")
    )
    with pytest.raises(IntegrationError) as safety:
        environment.set("heater", "on", None, cancel_event=None)
    assert safety.value.code == "safety_unconfigured"

    configured = _settings(require_fresh_sensor=False)
    environment = EnvironmentControlAdapter(
        configured, actions, state, Sensors("2026-08-15T12:00:00Z")
    )
    with pytest.raises(IntegrationError) as duration:
        environment.set("heater", "on", 2, cancel_event=None)
    assert duration.value.code == "invalid_arguments"


def test_host_status_uses_only_fixed_service_names(tmp_path) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "active\n2\n"

    def runner(argv, **_kwargs):
        calls.append(argv)
        return Result()

    adapter = HostStatusAdapter(BrokerSettings(), runner=runner, root=tmp_path)
    assert adapter.service() == {"state": "active", "nrestarts": 2}
    dependencies = adapter.dependencies()
    assert set(dependencies) == {"dashboard", "influxdb", "mqtt", "tailscale"}
    selected = {argv[2] for argv in calls}
    assert selected == {
        "butters-web.service",
        "home-sensor-dashboard.service",
        "influxdb.service",
        "mosquitto.service",
        "tailscaled.service",
    }
    assert all(argv[:2] == ["/usr/bin/systemctl", "show"] for argv in calls)


def test_timed_override_is_recorded_before_the_device_is_energised(tmp_path) -> None:
    """A crash between an accepted ON command and the record must stay recoverable.

    Recording the release obligation afterwards leaves a window in which the
    heater is energised but recover_overrides() has nothing to release.
    """

    state = ActionStateStore(tmp_path / "actions.sqlite3", _settings())
    observed: list[tuple[object, tuple[dict, ...]]] = []

    class ObservingActions:
        def __init__(self) -> None:
            self.operations: list[object] = []

        def execute(self, operation, *, cancel_event=None):
            self.operations.append(operation)
            observed.append((operation, state.overrides()))
            return {"accepted": True}

    actions = ObservingActions()
    current = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = EnvironmentControlAdapter(
        _settings(), actions, state, Sensors(current)
    )
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(IntegrationError) as released:
        environment.set("heater", "on", 1, cancel_event=cancelled, job_id="job-1")
    assert released.value.code == "cancelled"
    on_operation, overrides_at_on = observed[0]
    assert on_operation is BrokerOperation.HEATER_ON
    # The obligation is durable before the device can be energised.
    assert [item["device"] for item in overrides_at_on] == ["heater"]
    assert overrides_at_on[0]["job_id"] == "job-1"
    assert observed[-1][0] is BrokerOperation.HEATER_OFF
    assert state.overrides() == ()


def test_untimed_environment_control_records_no_override(tmp_path) -> None:
    state = ActionStateStore(tmp_path / "actions.sqlite3", _settings())
    actions = FixedActions()
    current = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    environment = EnvironmentControlAdapter(
        _settings(), actions, state, Sensors(current)
    )
    result = environment.set("heater", "on", None, cancel_event=None)
    assert result["duration_minutes"] is None
    assert state.overrides() == ()
    assert actions.operations == [BrokerOperation.HEATER_ON]
