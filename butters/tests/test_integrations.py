from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from butters.assistant_config import IntegrationSettings
from butters.integrations.dashboard import DashboardSensorAdapter
from butters.integrations.model import IntegrationError
from butters.integrations.server_health import (
    SERVICE_ALLOWLIST,
    LocalServerHealthAdapter,
)


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


PAYLOAD = b"""{
  "generated_at": "2026-08-11T12:00:00Z",
  "environment": [{
    "id": "1", "last_seen": "2026-08-11T11:59:50Z",
    "humidity": 18.4, "available_fields": ["humidity"]
  }],
  "air_quality": [],
  "nodes": [{
    "id": "1", "sensor_type": "environment", "status": "online",
    "age_seconds": 10
  }]
}"""


def test_dashboard_adapter_parses_and_caches_typed_snapshot() -> None:
    calls = 0

    def opener(*args: object, **kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        assert kwargs["timeout"] == 2.0
        return Response(PAYLOAD)

    settings = IntegrationSettings(timeout_seconds=2.0, cache_seconds=5.0)
    adapter = DashboardSensorAdapter(settings, opener=opener, clock=lambda: 100.0)

    first = adapter.snapshot()
    second = adapter.snapshot()

    assert first is second
    assert calls == 1
    assert first.records[0].values["humidity"] == 18.4
    assert first.records[0].age_seconds == 10


def test_dashboard_adapter_enforces_response_size() -> None:
    settings = IntegrationSettings(max_response_bytes=1024)
    adapter = DashboardSensorAdapter(
        settings, opener=lambda *args, **kwargs: Response(b"x" * 1025)
    )

    with pytest.raises(IntegrationError, match="too large") as raised:
        adapter.snapshot()

    assert raised.value.code == "invalid_response"


def test_dashboard_timeout_becomes_clean_integration_error() -> None:
    def timeout(*args: object, **kwargs: object):
        raise TimeoutError

    adapter = DashboardSensorAdapter(IntegrationSettings(), opener=timeout)

    with pytest.raises(IntegrationError, match="timed out") as raised:
        adapter.snapshot()

    assert raised.value.code == "timeout"


def test_server_health_commands_are_fixed_to_allowlist() -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(command)
        if command[0] == "systemctl":
            return SimpleNamespace(
                stdout="\n".join("active" for _ in SERVICE_ALLOWLIST), returncode=0
            )
        return SimpleNamespace(stdout="throttled=0x0\n", returncode=0)

    health = LocalServerHealthAdapter(runner=runner).snapshot()

    assert ["vcgencmd", "get_throttled"] in calls
    assert [
        "systemctl",
        "is-active",
        *(unit for _, unit in SERVICE_ALLOWLIST),
    ] in calls
    assert all(service.active for service in health.services)
    assert health.throttled == "0x0"
