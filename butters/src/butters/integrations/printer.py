"""Bounded read-only access to dashboard printer endpoints."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from butters.assistant_config import IntegrationSettings
from butters.integrations.model import (
    IntegrationError,
    PrintEnvironmentSnapshot,
    PrinterSnapshot,
)

OpenUrl = Callable[..., Any]


class DashboardPrinterAdapter:
    def __init__(
        self,
        settings: IntegrationSettings,
        *,
        opener: OpenUrl = urllib.request.urlopen,
    ) -> None:
        self.settings = settings
        self._opener = opener

    def current(self) -> PrinterSnapshot:
        payload = self._fetch("/api/printer")
        if payload.get("status") == "not_configured":
            raise IntegrationError("unavailable", "printer observer is not configured")
        printer_id = _required_string(payload, "printer_id")
        printer_model = _required_string(payload, "printer_model")
        state = _required_string(payload, "normalized_state")
        provenance = payload.get("provenance", {})
        if not isinstance(provenance, Mapping):
            provenance = {}
        return PrinterSnapshot(
            printer_id=printer_id,
            printer_model=printer_model,
            online=payload.get("online") is True,
            normalized_state=state,
            observed_at=_optional_string(payload.get("observed_at")),
            values=dict(payload),
            provenance={
                str(key): str(value)
                for key, value in provenance.items()
                if isinstance(key, str) and isinstance(value, str)
            },
        )

    def environment_summary(self) -> PrintEnvironmentSnapshot:
        payload = self._fetch("/api/printer/environment-summary")
        session = payload.get("session", {})
        metrics = payload.get("metrics", {})
        if not isinstance(session, Mapping) or not isinstance(metrics, Mapping):
            raise IntegrationError(
                "invalid_response", "printer summary has an invalid shape"
            )
        parsed_metrics: dict[str, dict[str, float | int | None]] = {}
        for metric, values in metrics.items():
            if not isinstance(metric, str) or not isinstance(values, Mapping):
                continue
            parsed_metrics[metric] = {
                str(key): value
                for key, value in values.items()
                if isinstance(key, str)
                and (
                    value is None
                    or (isinstance(value, (int, float)) and not isinstance(value, bool))
                )
            }
        recovery = payload.get("voc_recovery_seconds")
        return PrintEnvironmentSnapshot(
            available=payload.get("available") is True,
            reason=_optional_string(payload.get("reason")),
            observational=payload.get("observational") is True,
            session=dict(session),
            metrics=parsed_metrics,
            voc_recovery_seconds=(
                recovery
                if isinstance(recovery, int) and not isinstance(recovery, bool)
                else None
            ),
        )

    def _fetch(self, path: str) -> Mapping[str, Any]:
        request = urllib.request.Request(
            f"{self.settings.dashboard_url}{path}",
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
            raise IntegrationError("timeout", "printer query timed out") from exc
        except urllib.error.HTTPError as exc:
            raise IntegrationError(
                "upstream_status", f"dashboard returned HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise IntegrationError(
                "unavailable", "printer data is unavailable"
            ) from exc
        if len(raw) > self.settings.max_response_bytes:
            raise IntegrationError("invalid_response", "printer response was too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrationError(
                "invalid_response", "dashboard returned invalid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise IntegrationError(
                "invalid_response", "printer payload is not an object"
            )
        return payload


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise IntegrationError("invalid_response", f"printer payload has no {key}")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
