"""Run the NAS Agent with operator-owned config and file-mounted secrets."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from .backends import (
    JellyfinBackend,
    LocalStatusBackend,
    TailscaleMetricsBackend,
    TrueNasRpc,
)
from .bandwidth import BandwidthService, DryRunGovernor, NetworkTelemetry
from .client import Client, healthcheck
from .config import load_agent_credentials, load_api_key, load_config
from .engine import Engine


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            fields = json.loads(record.getMessage())
            allowed = {
                "event",
                "reason",
                "request_id",
                "action",
                "success",
                "duplicate",
                "count",
                "sequence",
                "method",
                "outcome",
                "address_kind",
                "elapsed_ms",
                "lock_wait_ms",
                "backend_ms",
                "send_ms",
                "ack_ms",
                "total_ms",
            }
            fields = {key: value for key, value in fields.items() if key in allowed}
        except (TypeError, ValueError, AttributeError):
            fields = {"event": "unstructured_diagnostic_suppressed"}
        return json.dumps({"timestamp": time.time(), **fields})


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--config", type=Path)
    value.add_argument("--agent-credentials", type=Path)
    value.add_argument("--truenas-read-key", type=Path)
    value.add_argument("--truenas-shutdown-key", type=Path)
    value.add_argument("--jellyfin-api-key", type=Path)
    value.add_argument("--healthcheck", type=Path)
    return value


def main() -> None:
    options = parser().parse_args()
    if options.healthcheck is not None:
        raise SystemExit(0 if healthcheck(options.healthcheck) else 1)
    if not all((options.config, options.agent_credentials, options.truenas_read_key)):
        raise SystemExit("config and credential file arguments are required")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(SafeJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        config = load_config(options.config)
        credentials = load_agent_credentials(options.agent_credentials)
        read_key = load_api_key(options.truenas_read_key, "truenas_read_key")
        shutdown_key = None
        if config.shutdown_enabled:
            if options.truenas_shutdown_key is None:
                raise ValueError("shutdown_credential_unavailable")
            shutdown_key = load_api_key(
                options.truenas_shutdown_key, "truenas_shutdown_key"
            )
        truenas = TrueNasRpc(config, read_key, shutdown_key)
        jellyfin_key = None
        if config.jellyfin_session_monitoring_enabled:
            if options.jellyfin_api_key is None:
                raise ValueError("jellyfin_credential_unavailable")
            jellyfin_key = load_api_key(options.jellyfin_api_key, "jellyfin_api_key")
        jellyfin = JellyfinBackend(config, jellyfin_key)
        network = NetworkTelemetry(config, truenas, TailscaleMetricsBackend(config))
        bandwidth = BandwidthService(network, jellyfin, DryRunGovernor(config))
        engine = Engine(LocalStatusBackend(), truenas, jellyfin, network, bandwidth)
        asyncio.run(Client(config, credentials, engine).run())
    except Exception:  # noqa: BLE001 - startup diagnostics never include secret values.
        logging.getLogger("butters_nas_agent").error(
            '{"event":"startup_failed","reason":"configuration_or_credential_unavailable"}'
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
