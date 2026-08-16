"""Bounded semantic sensor-history access through the existing dashboard API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from butters.assistant_config import IntegrationSettings
from butters.integrations.model import IntegrationError
from butters.routing.entities import EntityRegistry, MetricRegistry

LOOKBACKS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
BUCKET_SECONDS = {"auto": 0, "1m": 60, "15m": 900, "1h": 3600, "6h": 21600}
MAX_POINTS = 256


@dataclass(frozen=True, slots=True)
class HistorySeries:
    entity: str
    start: str
    end: str
    bucket: str
    metrics: tuple[str, ...]
    points: tuple[dict[str, object], ...]
    source_tier: str


class DashboardHistoryAdapter:
    def __init__(
        self,
        settings: IntegrationSettings,
        entities: EntityRegistry,
        metrics: MetricRegistry,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.entities = entities
        self.metrics = metrics
        self._opener = opener
        self._now = now

    def history(
        self,
        *,
        entity_id: str,
        metric_ids: tuple[str, ...],
        start: str | None,
        end: str | None,
        lookback: str | None,
        bucket: str = "auto",
        max_points: int = MAX_POINTS,
    ) -> HistorySeries:
        entity = self.entities.require(entity_id)
        if entity.sensor_type == "printer":
            raise IntegrationError(
                "policy_denied", "printer history requires a printer skill"
            )
        if not metric_ids or len(metric_ids) > 12:
            raise IntegrationError(
                "invalid_arguments", "one to twelve metrics are required"
            )
        fields: dict[str, str] = {}
        for metric_id in metric_ids:
            metric = self.metrics.require(metric_id)
            if entity.sensor_type not in metric.sensor_types:
                raise IntegrationError(
                    "policy_denied", "metric is not valid for this entity"
                )
            fields[metric.field] = metric_id
        if bucket not in BUCKET_SECONDS:
            raise IntegrationError("invalid_arguments", "bucket is not allow-listed")
        if not 1 <= max_points <= MAX_POINTS:
            raise IntegrationError(
                "invalid_arguments", "max_points exceeds the configured limit"
            )

        now = self._utc(self._now())
        if lookback is not None:
            if start is not None or end is not None or lookback not in LOOKBACKS:
                raise IntegrationError("invalid_arguments", "history window is invalid")
            start_time = now - LOOKBACKS[lookback]
            end_time = now
            range_key = lookback
        else:
            if start is None or end is None:
                raise IntegrationError(
                    "invalid_arguments", "start and end are required"
                )
            start_time = self._parse_time(start)
            end_time = self._parse_time(end)
            if end_time <= start_time or end_time > now + timedelta(minutes=5):
                raise IntegrationError(
                    "invalid_arguments", "history interval is invalid"
                )
            age = now - start_time
            if age > LOOKBACKS["30d"]:
                raise IntegrationError(
                    "lookback_limit", "history is limited to thirty days"
                )
            range_key = next(
                key for key in ("1h", "24h", "7d", "30d") if age <= LOOKBACKS[key]
            )

        query = {"range": range_key, "sensor_type": entity.sensor_type}
        query["node_id" if entity.sensor_type == "environment" else "location"] = (
            entity.source_id
        )
        payload = self._fetch("/api/readings?" + urllib.parse.urlencode(query))
        raw_series = payload.get("series", [])
        points: list[dict[str, object]] = []
        if isinstance(raw_series, list):
            for series in raw_series[:4]:
                if not isinstance(series, Mapping):
                    continue
                raw_points = series.get("points", [])
                if not isinstance(raw_points, list):
                    continue
                for point in raw_points[:MAX_POINTS]:
                    if not isinstance(point, Mapping):
                        continue
                    stamp = point.get("time")
                    if not isinstance(stamp, str):
                        continue
                    try:
                        parsed = self._parse_time(stamp)
                    except IntegrationError:
                        continue
                    if not start_time <= parsed <= end_time:
                        continue
                    item: dict[str, object] = {"time": self._iso(parsed)}
                    for field, metric_id in fields.items():
                        value = point.get(field)
                        if isinstance(value, (int, float)) and not isinstance(
                            value, bool
                        ):
                            item[metric_id] = float(value)
                    if len(item) > 1:
                        points.append(item)
        points.sort(key=lambda item: str(item["time"]))
        points = self._bucket(points, bucket)
        if len(points) > max_points:
            raise IntegrationError(
                "too_many_points", "history result exceeds max_points"
            )
        return HistorySeries(
            entity_id,
            self._iso(start_time),
            self._iso(end_time),
            bucket if bucket != "auto" else str(payload.get("window") or "auto"),
            metric_ids,
            tuple(points),
            str(payload.get("data_tier") or "dashboard_history"),
        )

    def _bucket(
        self, points: list[dict[str, object]], bucket: str
    ) -> list[dict[str, object]]:
        seconds = BUCKET_SECONDS[bucket]
        if seconds == 0:
            return points
        grouped: dict[int, dict[str, list[float]]] = {}
        for point in points:
            stamp = self._parse_time(str(point["time"]))
            key = int(stamp.timestamp()) // seconds * seconds
            target = grouped.setdefault(key, {})
            for metric, value in point.items():
                if metric != "time" and isinstance(value, (int, float)):
                    target.setdefault(metric, []).append(float(value))
        result = []
        for key in sorted(grouped):
            item: dict[str, object] = {
                "time": self._iso(datetime.fromtimestamp(key, timezone.utc))
            }
            for metric, values in sorted(grouped[key].items()):
                item[metric] = round(sum(values) / len(values), 6)
            result.append(item)
        return result

    def _fetch(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            self.settings.dashboard_url + path,
            headers={"Accept": "application/json", "User-Agent": "Butters/0.7"},
            method="GET",
        )
        try:
            with self._opener(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                raw = response.read(self.settings.max_response_bytes + 1)
                status = int(getattr(response, "status", 200))
        except TimeoutError as exc:
            raise IntegrationError("timeout", "sensor history query timed out") from exc
        except urllib.error.HTTPError as exc:
            raise IntegrationError(
                "upstream_status", "sensor history is unavailable"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(
                "unavailable", "sensor history is unavailable"
            ) from exc
        if status != 200 or len(raw) > self.settings.max_response_bytes:
            raise IntegrationError(
                "invalid_response", "sensor history response is invalid"
            )
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError(
                "invalid_response", "sensor history response is invalid"
            ) from exc
        if not isinstance(payload, Mapping):
            raise IntegrationError(
                "invalid_response", "sensor history response is invalid"
            )
        return payload

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntegrationError(
                "invalid_arguments", "timestamp must be ISO 8601"
            ) from exc
        if parsed.tzinfo is None:
            raise IntegrationError(
                "invalid_arguments", "timestamp must include a timezone"
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat().replace("+00:00", "Z")
