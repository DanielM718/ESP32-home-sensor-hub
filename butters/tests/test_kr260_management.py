from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from butters.actions.broker import (
    BrokerError,
    BrokerOperation,
    BrokerResult,
    FixedBrokerConfig,
    FixedBrokerOperations,
)
from butters.assistant_config import Kr260Settings, load_assistant_settings
from butters.integrations.kr260 import Kr260Observer

MACHINE_ID = "7a6ccc6abe67407b93558d1efcb36587"
TELEMETRY = f"""hostname=kria
machine_id={MACHINE_ID}
boot_id=63bb230d-e96c-438c-917a-dcc464a80a44
kernel=Linux 6.8.0-1015-xilinx
uptime=10860.37
load=0.08 0.02 0.01 1/180 10838
cpu_start=cpu  100 0 50 800 20 0 0 0 0 0
cpu_end=cpu  110 0 55 875 20 0 0 0 0 0
MemTotal:        3996384 kB
MemAvailable:    3572760 kB
SwapTotal:             0 kB
SwapFree:              0 kB
reboot_required=false
system_state=running
if_state=up
if_mac=00:0a:35:00:00:01
if_rx=1234
if_tx=5678
ipv4=2: eth1    inet 192.168.1.125/24 scope global eth1
ipv6=2: eth1    inet6 fd00::125/64 scope global
default_route=default via 192.168.1.1 dev eth1
soc_temp_millic=29687
"""


def settings(tmp_path: Path) -> Kr260Settings:
    del tmp_path
    return Kr260Settings(
        enabled=True,
        host="192.168.1.125",
        expected_machine_id=MACHINE_ID,
    ).validated()


class Runner:
    def __init__(self, *, ping=0):
        self.ping = ping
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, self.ping)


class Broker:
    def __init__(
        self,
        *,
        output: str = TELEMETRY,
        ok: bool = True,
        error_code: str | None = None,
    ) -> None:
        self.output = output
        self.ok = ok
        self.error_code = error_code
        self.calls = []

    def request(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        return BrokerResult(
            operation,
            self.ok,
            {"telemetry": self.output} if self.ok else {},
            self.error_code,
        )


def test_kr260_normal_observation_is_identity_verified_and_bounded(tmp_path) -> None:
    runner = Runner()
    broker = Broker()
    observed = Kr260Observer(
        settings(tmp_path), broker=broker, runner=runner
    ).observe(refresh=True)

    assert observed["operational_state"] == "online"
    assert observed["ssh_available"] is True
    assert observed["identity_verified"] is True
    assert observed["soc_temperature_c"] == 29.69
    assert observed["memory_utilization_percent"] == 10.6
    assert observed["cpu_utilization_percent"] == 16.67
    assert observed["network"]["interface"] == "eth1"
    assert len(runner.calls) == 1
    assert broker.calls[0][0] is BrokerOperation.KR260_STATUS


def test_kr260_unreachable_device_reports_offline_without_inference(tmp_path) -> None:
    observed = Kr260Observer(
        settings(tmp_path), broker=Broker(ok=False, error_code="operation_failed"),
        runner=Runner(ping=1),
    ).observe(refresh=True)

    assert observed["online"] is False
    assert observed["network_reachable"] is False
    assert observed["ssh_available"] is False
    assert observed["error"] == "ssh_failed"


def test_kr260_failed_ssh_is_distinct_from_network_reachability(tmp_path) -> None:
    observed = Kr260Observer(
        settings(tmp_path), broker=Broker(ok=False, error_code="operation_failed"),
        runner=Runner(ping=0),
    ).observe(refresh=True)

    assert observed["online"] is True
    assert observed["network_reachable"] is True
    assert observed["ssh_available"] is False
    assert observed["identity_verified"] is False


def test_kr260_wrong_machine_identity_fails_closed(tmp_path) -> None:
    observed = Kr260Observer(
        settings(tmp_path),
        broker=Broker(output=TELEMETRY.replace(MACHINE_ID, "0" * 32)),
        runner=Runner(),
    ).observe(refresh=True)

    assert observed["operational_state"] == "identity_failed"
    assert observed["ssh_available"] is True
    assert observed["identity_verified"] is False
    assert observed["error"] == "identity_mismatch"


def test_kr260_observation_cache_avoids_background_polling(tmp_path) -> None:
    runner = Runner()
    broker = Broker()
    observer = Kr260Observer(settings(tmp_path), broker=broker, runner=runner)
    observer.observe()
    observer.observe()
    assert len(runner.calls) == 1
    assert len(broker.calls) == 1


def test_deployed_observer_gets_telemetry_from_broker_without_reading_key(
    tmp_path,
) -> None:
    broker = Broker()
    runner = Runner()
    observed = Kr260Observer(
        settings(tmp_path), broker=broker, runner=runner
    ).observe(refresh=True)

    assert observed["identity_verified"] is True
    assert broker.calls[0][0] is BrokerOperation.KR260_STATUS
    assert len(runner.calls) == 1
    assert runner.calls[0][0][0] == "/usr/bin/ping"


def test_broker_status_uses_fixed_command_and_rejects_wrong_identity(tmp_path) -> None:
    outputs = [TELEMETRY, TELEMETRY.replace(MACHINE_ID, "0" * 32)]
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0), "")

    operations = FixedBrokerOperations(
        FixedBrokerConfig(
            desktop_host="desktop",
            desktop_user="Daniel",
            desktop_mac="00:00:00:00:00:00",
            desktop_broadcast="192.0.2.255",
            desktop_key=tmp_path / "desktop-key",
            kr260_host="192.168.1.125",
            kr260_key=tmp_path / "id_ed25519",
            kr260_expected_machine_id=MACHINE_ID,
            enabled_operations=frozenset({BrokerOperation.KR260_STATUS}),
        ),
        runner=runner,
    )

    status = operations.kr260_status()
    assert status["identity_verified"] is True
    assert calls[0][-1].startswith("LC_ALL=C")
    with pytest.raises(BrokerError) as error:
        operations.kr260_status()
    assert error.value.code == "identity_mismatch"


def test_failed_kr260_reboot_stops_after_identity_check(tmp_path) -> None:
    calls = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, MACHINE_ID + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "authentication required")

    operations = FixedBrokerOperations(
        FixedBrokerConfig(
            desktop_host="desktop",
            desktop_user="Daniel",
            desktop_mac="00:00:00:00:00:00",
            desktop_broadcast="192.0.2.255",
            desktop_key=tmp_path / "desktop-key",
            kr260_host="192.168.1.125",
            kr260_key=tmp_path / "id_ed25519",
            kr260_expected_machine_id=MACHINE_ID,
            enabled_operations=frozenset({BrokerOperation.KR260_REBOOT}),
        ),
        runner=runner,
    )

    with pytest.raises(BrokerError) as error:
        operations.kr260_reboot()
    assert error.value.code == "operation_failed"
    assert len(calls) == 2
    assert calls[0][-1] == "/usr/bin/cat /etc/machine-id"
    assert calls[1][-1] == "/usr/bin/systemctl --no-wall --no-block reboot"


def test_shutdown_and_jtag_reset_stay_unavailable_when_recovery_is_unverified(
    tmp_path,
) -> None:
    base = load_assistant_settings()
    observer = Kr260Observer(replace(base.kr260, enabled=False), broker=Broker())
    from butters.integrations.actions import FixedActionAdapter, Kr260Adapter

    adapter = Kr260Adapter(
        observer,
        FixedActionAdapter(object()),  # not reached while both gates are closed
        base.actions.kr260_reboot,
    )
    capability = adapter.status()["capability"]
    assert capability["shutdown"] is False
    assert capability["jtag_reset"] is False
    assert capability["reboot"] is False
