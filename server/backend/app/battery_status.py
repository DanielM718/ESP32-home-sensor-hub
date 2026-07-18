"""SHT41 battery status-bit definitions and decoding helpers."""

from __future__ import annotations


STATUS_BATTERY_OK = 1 << 2
STATUS_BATTERY_LOW = 1 << 3
STATUS_BATTERY_SHUTDOWN = 1 << 4


def decode_battery_status(status_flags: int | None) -> dict[str, bool | None]:
    """Decode known battery bits while preserving unavailable status as ``None``."""

    if status_flags is None:
        return {
            "battery_measurement_ok": None,
            "battery_low": None,
            "battery_shutdown": None,
        }

    return {
        "battery_measurement_ok": bool(status_flags & STATUS_BATTERY_OK),
        "battery_low": bool(status_flags & STATUS_BATTERY_LOW),
        "battery_shutdown": bool(status_flags & STATUS_BATTERY_SHUTDOWN),
    }
