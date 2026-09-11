"""Production-equivalent InfluxDB readiness probe."""

from __future__ import annotations

import time

from app.config import configure_logging, load_settings
from app.queries import InfluxReadRepository


def main() -> int:
    """Run the same durable inventory reconstruction required at app startup."""

    started = time.monotonic()
    settings = load_settings()
    configure_logging(settings.log_level)
    repository = InfluxReadRepository(settings.influx)
    repository.close()
    print(
        "InfluxDB durable inventory query succeeded "
        f"in {time.monotonic() - started:.3f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
