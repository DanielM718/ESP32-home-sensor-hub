"""JSON payload validation for incoming MQTT messages."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping


class ValidationError(ValueError):
    """Raised when an incoming MQTT message does not match the data contract."""


def parse_json_object(payload: bytes, *, max_payload_bytes: int) -> dict[str, Any]:
    """Decode and parse an MQTT payload as a JSON object."""

    if len(payload) > max_payload_bytes:
        raise ValidationError(
            f"payload is {len(payload)} bytes; maximum is {max_payload_bytes}"
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("payload is not valid UTF-8") from exc

    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"payload is not valid JSON: {exc.msg}") from exc

    if not isinstance(decoded, dict):
        raise ValidationError("payload must be a JSON object")

    return decoded


def required_int(
    data: Mapping[str, Any],
    key: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    value = _required(data, key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key} must be an integer")

    if min_value is not None and value < min_value:
        raise ValidationError(f"{key} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValidationError(f"{key} must be <= {max_value}")
    return value


def optional_int(
    data: Mapping[str, Any],
    key: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int | None:
    """Validate an integer field when present, preserving absence as ``None``."""

    if key not in data:
        return None

    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{key} must be an integer")

    if min_value is not None and value < min_value:
        raise ValidationError(f"{key} must be >= {min_value}")
    if max_value is not None and value > max_value:
        raise ValidationError(f"{key} must be <= {max_value}")
    return value


def required_float(
    data: Mapping[str, Any],
    key: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    value = _required(data, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{key} must be a number")

    result = float(value)
    if not math.isfinite(result):
        raise ValidationError(f"{key} must be finite")
    if min_value is not None and result < min_value:
        raise ValidationError(f"{key} must be >= {min_value}")
    if max_value is not None and result > max_value:
        raise ValidationError(f"{key} must be <= {max_value}")
    return result


def _required(data: Mapping[str, Any], key: str) -> Any:
    if key not in data:
        raise ValidationError(f"missing required field: {key}")
    return data[key]
