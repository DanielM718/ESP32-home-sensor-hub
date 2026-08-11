"""Bounded read-only access to the established dashboard latest-value API."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from butters.assistant_config import IntegrationSettings
from butters.integrations.model import IntegrationError, SensorRecord, SensorSnapshot

OpenUrl = Callable[..., Any]


class DashboardSensorAdapter:
    """Fetch the existing source-of-truth response with bounds and a short cache.

    This adapter intentionally knows the HTTP implementation. Routers and skills
    receive only typed snapshots and never receive a URL, token, or raw client.
    """

    def __init__(
        self,
        settings: IntegrationSettings,
        *,
        opener: OpenUrl = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._opener = opener
        self._clock = clock
        self._cached: SensorSnapshot | None = None
        self._cached_at = 0.0

    def snapshot(self) -> SensorSnapshot:
        now = self._clock()
        if (
            self._cached is not None
            and now - self._cached_at <= self.settings.cache_seconds
        ):
            return self._cached
        request = urllib.request.Request(
            f"{self.settings.dashboard_url}/api/latest",
            headers={"Accept": "application/json", "User-Agent": "Butters/0.4"},
            method="GET",
        )
        try:
            with self._opener(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise IntegrationError(
                        "upstream_status", f"dashboard returned HTTP {status}"
                    )
                raw = response.read(self.settings.max_response_bytes + 1)
        except IntegrationError:
            raise
        except TimeoutError as exc:
            raise IntegrationError("timeout", "dashboard query timed out") from exc
        except urllib.error.HTTPError as exc:
            raise IntegrationError(
                "upstream_status", f"dashboard returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(
                "unavailable", "dashboard sensor data is unavailable"
            ) from exc
        if len(raw) > self.settings.max_response_bytes:
            raise IntegrationError(
                "invalid_response", "dashboard response was too large"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError(
                "invalid_response", "dashboard returned invalid JSON"
            ) from exc
        snapshot = self._parse(payload)
        self._cached = snapshot
        self._cached_at = now
        return snapshot

    @staticmethod
    def _parse(payload: Any) -> SensorSnapshot:
        if not isinstance(payload, Mapping):
            raise IntegrationError(
                "invalid_response", "dashboard payload is not an object"
            )
        generated_at = payload.get("generated_at")
        if not isinstance(generated_at, str) or not generated_at:
            raise IntegrationError(
                "invalid_response", "dashboard payload has no generated_at"
            )
        node_map: dict[tuple[str, str], Mapping[str, Any]] = {}
        raw_nodes = payload.get("nodes", [])
        if isinstance(raw_nodes, list):
            for node in raw_nodes:
                if not isinstance(node, Mapping):
                    continue
                sensor_type = node.get("sensor_type")
                source_id = node.get("id")
                if isinstance(sensor_type, str) and source_id is not None:
                    node_map[(sensor_type, str(source_id))] = node

        records: list[SensorRecord] = []
        for sensor_type, collection_name in (
            ("environment", "environment"),
            ("air_quality", "air_quality"),
        ):
            items = payload.get(collection_name, [])
            if not isinstance(items, list):
                raise IntegrationError(
                    "invalid_response", f"dashboard {collection_name} is not an array"
                )
            for item in items:
                if not isinstance(item, Mapping) or item.get("id") is None:
                    continue
                source_id = str(item["id"])
                node = node_map.get((sensor_type, source_id), {})
                age = node.get("age_seconds")
                records.append(
                    SensorRecord(
                        sensor_type=sensor_type,
                        source_id=source_id,
                        last_seen=_optional_string(item.get("last_seen")),
                        age_seconds=(
                            int(age)
                            if isinstance(age, int) and not isinstance(age, bool)
                            else None
                        ),
                        status=str(node.get("status") or "unknown"),
                        values=dict(item),
                        available_fields=tuple(
                            str(field)
                            for field in item.get("available_fields", [])
                            if isinstance(field, str)
                        ),
                    )
                )
        return SensorSnapshot(generated_at, tuple(records))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
