"""Cross-process coordination for expensive InfluxDB startup queries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import logging
import os
from pathlib import Path
import time


LOGGER = logging.getLogger("home_sensor.influx_startup")
DEFAULT_LOCK_PATH = Path("/var/lib/home-sensor/influx-startup-query.lock")


@contextmanager
def serialized_inventory_query() -> Iterator[None]:
    """Serialize the durable inventory scans performed during process startup."""

    lock_path = Path(
        os.getenv("INFLUXDB_STARTUP_LOCK_PATH", str(DEFAULT_LOCK_PATH))
    )
    started = time.monotonic()
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        waited = time.monotonic() - started
        if waited >= 0.1:
            LOGGER.info(
                "Waited %.3fs for exclusive durable inventory startup query",
                waited,
            )
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
