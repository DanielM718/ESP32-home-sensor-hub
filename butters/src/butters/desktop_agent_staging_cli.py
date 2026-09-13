"""Fixed-command local client for Desktop Agent staging validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from butters_agent.protocol import NAME

from butters.desktop_agent_staging import (
    load_staging_settings,
    validate_staging_settings,
)


def _app(value: str) -> str:
    if NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("app must be a symbolic identifier")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate the isolated staging Desktop Agent"
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("state", help="inspect authenticated agent state")
    commands.add_parser("list-apps", help="list safe symbolic applications")
    status = commands.add_parser("status", help="inspect one symbolic application")
    status.add_argument("app", type=_app)
    launch = commands.add_parser(
        "launch", help="freeze, authorize, and execute one staging launch"
    )
    launch.add_argument("app", type=_app)
    return result


def main() -> int:
    options = parser().parse_args()
    config = Path(
        os.environ.get(
            "BUTTERS_STAGING_CONFIG", "/etc/butters-staging/assistant.toml"
        )
    )
    settings = load_staging_settings(config)
    validate_staging_settings(config, settings)
    paths = {
        "state": ("GET", "/validation/v1/state"),
        "list-apps": ("POST", "/validation/v1/list-apps"),
        "status": ("POST", f"/validation/v1/status/{options.app}"),
        "launch": ("POST", f"/validation/v1/launch/{options.app}"),
    }
    method, path = paths[options.command]
    request = Request(
        f"http://127.0.0.1:{settings.port}{path}",
        method=method,
        headers={"Content-Length": "0"},
    )
    try:
        with urlopen(
            request, timeout=settings.agent_ingress.request_timeout_seconds + 12
        ) as response:
            payload = json.load(response)
    except HTTPError as exc:
        payload = json.load(exc)
    except (OSError, URLError, ValueError):
        print(json.dumps({"ok": False, "error": "staging_unavailable"}))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
