"""Narrow read-only adapters for home data and local host health."""

from butters.integrations.dashboard import DashboardSensorAdapter
from butters.integrations.server_health import LocalServerHealthAdapter

__all__ = ["DashboardSensorAdapter", "LocalServerHealthAdapter"]
