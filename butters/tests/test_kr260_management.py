from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from butters.actions.broker import (
    _KR260_REBOOT_COMMAND,
    BrokerError,
    BrokerOperation,
    BrokerResult,
    FixedBrokerConfig,
    FixedBrokerOperations,
)
from butters.assistant_config import Kr260Settings, load_assistant_settings
from butters.integrations.actions import Kr260Adapter
from butters.integrations.kr260 import Kr260Observer
from butters.integrations.model import IntegrationError

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
        # What an unprovisioned board answers to `sudo -n`.
        return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required\n")

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
    assert error.value.code == "reboot_unauthorized"
    assert len(calls) == 2
    assert calls[0][-1] == "/usr/bin/cat /etc/machine-id"
    # Exactly the argv the board's sudoers rule grants; never a bare systemctl,
    # which polkit denies from a non-interactive SSH session.
    assert calls[1][-1] == (
        "/usr/bin/sudo -n /usr/bin/systemctl --no-wall --no-block reboot"
    )


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


# --- Failure classification, bounded probing and reboot authorization -------

BUTTERS_ROOT = Path(__file__).resolve().parents[1]


def _broker_operations(tmp_path, runner, *operations):
    return FixedBrokerOperations(
        FixedBrokerConfig(
            desktop_host="desktop",
            desktop_user="Daniel",
            desktop_mac="00:00:00:00:00:00",
            desktop_broadcast="192.0.2.255",
            desktop_key=tmp_path / "desktop-key",
            kr260_host="192.168.1.125",
            kr260_key=tmp_path / "id_ed25519",
            kr260_expected_machine_id=MACHINE_ID,
            enabled_operations=frozenset(operations),
        ),
        runner=runner,
    )


@pytest.mark.parametrize(
    ("returncode", "stderr", "code"),
    (
        (255, "ssh: connect to host 192.168.1.125 port 22: No route to host", "host_unreachable"),
        (255, "ssh: connect to host 192.168.1.125 port 22: Connection timed out", "host_unreachable"),
        (255, "ssh: connect to host 192.168.1.125 port 22: Connection refused", "host_unreachable"),
        (255, "ubuntu@192.168.1.125: Permission denied (publickey).", "ssh_auth_failed"),
        (255, "Host key verification failed.", "host_key_mismatch"),
        (255, "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", "host_key_mismatch"),
        (255, "kex_exchange_identification: read: Connection reset", "ssh_failed"),
        (1, "cat: /etc/machine-id: No such file or directory", "operation_failed"),
    ),
)
def test_kr260_ssh_failures_map_to_fixed_codes(tmp_path, returncode, stderr, code) -> None:
    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, returncode, "", stderr)

    with pytest.raises(BrokerError) as error:
        _broker_operations(tmp_path, runner, BrokerOperation.KR260_STATUS).kr260_status()

    assert error.value.code == code
    # Remote text is only classified, never carried in the error.
    assert stderr not in str(error.value)


def test_kr260_ssh_is_bounded_by_connect_and_process_timeouts(tmp_path) -> None:
    seen = {}

    def runner(argv, **kwargs):
        seen.update(argv=list(argv), timeout=kwargs["timeout"])
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(BrokerError) as error:
        _broker_operations(tmp_path, runner, BrokerOperation.KR260_STATUS).kr260_status()

    assert error.value.code == "operation_timeout"
    assert "ConnectTimeout=5" in seen["argv"]
    assert "StrictHostKeyChecking=yes" in seen["argv"]
    assert seen["timeout"] == 10


def test_kr260_reboot_is_refused_without_the_sudoers_grant(tmp_path) -> None:
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv[-1])
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 0, MACHINE_ID + "\n", "")
        return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")

    with pytest.raises(BrokerError) as error:
        _broker_operations(tmp_path, runner, BrokerOperation.KR260_REBOOT).kr260_reboot()

    assert error.value.code == "reboot_unauthorized"
    assert calls[1] == _KR260_REBOOT_COMMAND


def test_kr260_reboot_never_runs_against_a_different_board(tmp_path) -> None:
    calls = []

    def runner(argv, **_kwargs):
        calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, 0, "0" * 32 + "\n", "")

    with pytest.raises(BrokerError) as error:
        _broker_operations(tmp_path, runner, BrokerOperation.KR260_REBOOT).kr260_reboot()

    assert error.value.code == "identity_mismatch"
    assert calls == ["/usr/bin/cat /etc/machine-id"]


def test_sudoers_artifact_grants_exactly_the_broker_reboot_argv() -> None:
    rules = [
        line.strip()
        for line in (BUTTERS_ROOT / "config/kr260/butters-kr260-reboot.sudoers")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    granted = _KR260_REBOOT_COMMAND.removeprefix("/usr/bin/sudo -n ")

    assert rules == [f"ubuntu ALL=(root) NOPASSWD: {granted}"]
    for forbidden in ("*", "ALL:", "poweroff", "halt", "shutdown", "/bin/sh", "bash"):
        assert forbidden not in rules[0].split("NOPASSWD:", 1)[1]


def test_broker_audits_kr260_failures_without_remote_text(tmp_path, caplog) -> None:
    import json
    import os
    import struct

    from butters.actions.broker import BrokerServer

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 255, "", "ubuntu@kria: Permission denied (publickey). SECRET-TEXT"
        )

    operations = _broker_operations(tmp_path, runner, BrokerOperation.KR260_STATUS)
    server = BrokerServer(operations.handlers(), expected_uid=os.getuid())

    class Connection:
        def __init__(self) -> None:
            self.payload = bytearray(
                json.dumps(
                    {
                        "version": 1,
                        "request_id": "kr260_audit_request_1",
                        "operation": "kr260.status",
                    }
                ).encode()
                + b"\n"
            )
            self.output = bytearray()

        def settimeout(self, _timeout) -> None:
            return None

        def getsockopt(self, *_args):
            return struct.pack("3i", 1, os.getuid(), 1)

        def recv(self, count):
            value = bytes(self.payload[:count])
            del self.payload[:count]
            return value

        def sendall(self, value) -> None:
            self.output.extend(value)

    connection = Connection()
    with caplog.at_level("INFO"):
        server.handle(connection)  # type: ignore[arg-type]

    response = json.loads(connection.output)
    assert response["ok"] is False
    assert response["error_code"] == "ssh_auth_failed"
    journal = "\n".join(record.getMessage() for record in caplog.records)
    assert "kr260.status" in journal
    assert "ssh_auth_failed" in journal
    assert "SECRET-TEXT" not in journal and "Permission denied" not in journal


@pytest.mark.parametrize(
    ("code", "error", "state", "answered"),
    (
        ("host_unreachable", "host_unreachable", "offline", False),
        ("ssh_auth_failed", "ssh_auth_failed", "offline", False),
        ("operation_timeout", "ssh_timeout", "offline", False),
        ("host_key_mismatch", "host_key_mismatch", "identity_failed", True),
        ("identity_mismatch", "identity_mismatch", "identity_failed", True),
        ("operation_unavailable", "capability_disabled", "disabled", False),
        ("something_new", "ssh_failed", "offline", False),
    ),
)
def test_kr260_observer_reports_each_failure_distinctly(
    tmp_path, code, error, state, answered
) -> None:
    observed = Kr260Observer(
        settings(tmp_path),
        broker=Broker(ok=False, error_code=code),
        runner=Runner(ping=1),
    ).observe(refresh=True)

    assert observed["error"] == error
    assert observed["operational_state"] == state
    assert observed["ssh_available"] is answered
    assert observed["identity_verified"] is False


@pytest.mark.parametrize(
    "telemetry",
    ("", "garbage\x00\nmore=", TELEMETRY.replace("hostname=kria", "hostname=evil")),
)
def test_kr260_malformed_telemetry_never_verifies_identity(tmp_path, telemetry) -> None:
    observed = Kr260Observer(
        settings(tmp_path), broker=Broker(output=telemetry), runner=Runner()
    ).observe(refresh=True)

    assert observed["identity_verified"] is False
    assert observed["operational_state"] == "identity_failed"


def test_kr260_non_text_telemetry_is_malformed(tmp_path) -> None:
    class OddBroker(Broker):
        def request(self, operation, **kwargs):
            self.calls.append((operation, kwargs))
            return BrokerResult(operation, True, {"telemetry": 42}, None)

    observed = Kr260Observer(
        settings(tmp_path), broker=OddBroker(), runner=Runner()
    ).observe(refresh=True)
    assert observed["error"] == "malformed_result"


def test_kr260_unreachable_broker_is_offline_not_an_exception(tmp_path) -> None:
    class DownBroker(Broker):
        def request(self, operation, **kwargs):
            raise BrokerError("broker_unavailable", "action broker is unavailable")

    observed = Kr260Observer(
        settings(tmp_path), broker=DownBroker(), runner=Runner(ping=1)
    ).observe(refresh=True)
    assert observed["operational_state"] == "offline"
    assert observed["error"] == "ssh_unavailable"


class Monotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_explicit_refresh_cannot_hammer_an_offline_board(tmp_path) -> None:
    clock = Monotonic()
    broker = Broker(ok=False, error_code="host_unreachable")
    runner = Runner(ping=1)
    observer = Kr260Observer(
        settings(tmp_path), broker=broker, runner=runner, monotonic=clock
    )

    for _ in range(20):
        observer.observe(refresh=True)
    assert len(broker.calls) == len(runner.calls) == 1

    clock.now += 10.0
    observer.observe(refresh=True)
    assert len(broker.calls) == 2


def test_kr260_recovers_after_temporary_network_loss(tmp_path) -> None:
    clock = Monotonic()
    broker = Broker(ok=False, error_code="host_unreachable")
    observer = Kr260Observer(
        settings(tmp_path), broker=broker, runner=Runner(ping=1), monotonic=clock
    )
    assert observer.observe()["operational_state"] == "offline"

    # Back on the network: the next scheduled observation sees it, no retry loop.
    broker.ok, broker.error_code = True, None
    clock.now += 30.0
    assert observer.observe()["operational_state"] == "offline"  # still cached
    clock.now += 15.0
    recovered = observer.observe()
    assert recovered["operational_state"] == "online"
    assert recovered["identity_verified"] is True
    assert len(broker.calls) == 2


class Actions:
    def __init__(self, failure: str | None = None) -> None:
        self.failure = failure
        self.calls = []

    def execute(self, operation, *, cancel_event=None):
        self.calls.append(operation)
        if self.failure is not None:
            raise IntegrationError(self.failure, "fixed broker operation failed")
        return {"operation": operation.value, "accepted": True}


def _adapter(tmp_path, actions, broker=None, *, enabled=True):
    clock = Monotonic()
    base = load_assistant_settings()
    reboot = replace(base.actions.kr260_reboot, enabled=enabled, configured=enabled)
    observer = Kr260Observer(
        settings(tmp_path), broker=broker or Broker(), runner=Runner(), monotonic=clock
    )
    return Kr260Adapter(observer, actions, reboot, monotonic=clock), clock


def test_disabled_reboot_takes_no_probe_or_action(tmp_path) -> None:
    actions = Actions()
    broker = Broker()
    adapter, _clock = _adapter(tmp_path, actions, broker, enabled=False)

    with pytest.raises(IntegrationError) as error:
        adapter.reboot()

    assert error.value.code == "capability_unavailable"
    assert actions.calls == [] and broker.calls == []


def test_reboot_requires_a_freshly_verified_identity(tmp_path) -> None:
    actions = Actions()
    adapter, _clock = _adapter(
        tmp_path, actions, Broker(ok=False, error_code="host_unreachable")
    )

    with pytest.raises(IntegrationError) as error:
        adapter.reboot()

    assert error.value.code == "identity_unverified"
    assert actions.calls == []


def test_repeated_reboot_is_refused_until_the_boot_changes(tmp_path) -> None:
    actions = Actions()
    broker = Broker()
    adapter, clock = _adapter(tmp_path, actions, broker)

    first = adapter.reboot()
    assert first["previous_boot_id"] == "63bb230d-e96c-438c-917a-dcc464a80a44"

    clock.now += 11.0
    with pytest.raises(IntegrationError) as again:
        adapter.reboot()
    assert again.value.code == "reboot_in_progress"
    assert len(actions.calls) == 1

    # The board came back with a new boot: the reboot is observed to have
    # happened, and a new explicit request is allowed.
    broker.output = TELEMETRY.replace(
        "63bb230d-e96c-438c-917a-dcc464a80a44", "11111111-2222-3333-4444-555555555555"
    )
    clock.now += 11.0
    status = adapter.status(refresh=True)
    assert status["last_reboot_request"]["boot_changed"] is True
    adapter.reboot()
    assert len(actions.calls) == 2


def test_unsent_reboot_failure_does_not_block_a_retry(tmp_path) -> None:
    actions = Actions(failure="reboot_unauthorized")
    adapter, clock = _adapter(tmp_path, actions)

    with pytest.raises(IntegrationError):
        adapter.reboot()
    clock.now += 11.0
    with pytest.raises(IntegrationError) as again:
        adapter.reboot()

    assert again.value.code == "reboot_unauthorized"
    assert len(actions.calls) == 2
    assert "last_reboot_request" not in adapter.status()


def test_reboot_with_an_unknown_outcome_is_treated_as_possibly_sent(tmp_path) -> None:
    actions = Actions(failure="operation_timeout")
    adapter, clock = _adapter(tmp_path, actions)

    with pytest.raises(IntegrationError):
        adapter.reboot()
    clock.now += 11.0
    with pytest.raises(IntegrationError) as again:
        adapter.reboot()

    assert again.value.code == "reboot_in_progress"
    assert len(actions.calls) == 1


def test_kr260_capability_gates_ship_closed() -> None:
    base = load_assistant_settings()
    assert base.kr260.enabled is False
    assert base.actions.kr260_reboot.enabled is False
    assert base.actions.kr260_reboot.configured is False
    example = (BUTTERS_ROOT / "config/action-broker.example.toml").read_text()
    assert '"kr260.status" = false' in example
    assert '"kr260.reboot" = false' in example
    assert "kr260.shutdown" not in example
    assert "kr260.shutdown" not in {operation.value for operation in BrokerOperation}
