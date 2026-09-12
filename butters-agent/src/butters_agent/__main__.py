"""Run the Windows agent with an operator-owned local configuration file."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import tomllib

from .client import Client
from .engine import Engine

LOG = logging.getLogger("butters_agent")


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
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
                "sha256",
            }
            fields = {key: value for key, value in fields.items() if key in allowed}
        except (ValueError, AttributeError):
            fields = {"event": "unstructured_diagnostic_suppressed"}
        return json.dumps({"timestamp": time.time(), **fields})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    if os.name != "nt":
        raise SystemExit(
            "Interactive agent requires Windows; use fake backend in tests"
        )

    from .platform.win32 import Platform, dpapi

    platform = Platform()
    platform.validate_registry(options.config)
    config = tomllib.loads(options.config.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1:
        raise SystemExit("invalid_configuration")
    local = Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent"
    local.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        local / "agent.jsonl",
        maxBytes=1048576,
        backupCount=3,
    )
    handler.setFormatter(SafeJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        encrypted = (local / "credentials.dpapi").read_bytes()
        credentials = json.loads(dpapi(encrypted, protect=False))
        registry = options.config.parent / "apps.toml"
        engine = Engine(platform, registry)
        LOG.info(
            json.dumps(
                {
                    "event": "registry_loaded",
                    "sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
                }
            )
        )
        asyncio.run(Client(config, credentials, engine).run())
    except Exception:  # noqa: BLE001 - diagnostics must redact unknown failures.
        LOG.error(
            '{"event":"startup_failed",'
            '"reason":"configuration_or_credential_unavailable"}'
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
