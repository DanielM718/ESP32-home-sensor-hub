"""pythonw -m butters_agent --config <operator-owned config>."""

import argparse
import asyncio
import json
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tomllib
import time

from .client import Client
from .engine import Engine


class SafeJsonFormatter(logging.Formatter):
    def format(self, record):
        try:
            fields = json.loads(record.getMessage())
            allowed = {"event", "reason", "request_id", "action", "success", "duplicate", "count", "sha256"}
            fields = {key: value for key, value in fields.items() if key in allowed}
        except (ValueError, AttributeError):
            fields = {"event": "unstructured_diagnostic_suppressed"}
        return json.dumps({"timestamp": time.time(), **fields})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    options = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("Interactive agent requires Windows; use fake backend in tests")
    from .platform.win32 import Platform, dpapi
    platform = Platform()
    platform.validate_registry(options.config)
    config = tomllib.loads(options.config.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1:
        raise SystemExit("invalid_configuration")
    local = Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent"
    local.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(local / "agent.jsonl", maxBytes=1048576, backupCount=3)
    handler.setFormatter(SafeJsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    try:
        credentials = json.loads(dpapi((local / "credentials.dpapi").read_bytes(), protect=False))
        engine = Engine(platform, options.config.parent / "apps.toml")
        logging.info(json.dumps({"event":"registry_loaded", "sha256":hashlib.sha256(
            (options.config.parent / "apps.toml").read_bytes()).hexdigest()}))
        asyncio.run(Client(config, credentials, engine).run())
    except Exception:
        logging.error('{"event":"startup_failed","reason":"configuration_or_credential_unavailable"}')
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
