"""Fixed-command local client for Desktop Agent staging validation."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sys
from pathlib import Path

from butters_agent.protocol import NAME

from butters.assistant_config import AgentIngressSettings
from butters.desktop_agent_staging import (
    STAGING_VALIDATION_SOCKET,
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


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.unix_path))


def _selected_route(options: argparse.Namespace) -> tuple[str, str]:
    if options.command == "state":
        return "GET", "/validation/v1/state"
    if options.command == "list-apps":
        return "POST", "/validation/v1/list-apps"
    if options.command == "status":
        return "POST", f"/validation/v1/status/{options.app}"
    return "POST", f"/validation/v1/launch/{options.app}"


def _request(method: str, path: str, timeout: float) -> dict[str, object]:
    connection = UnixHTTPConnection(STAGING_VALIDATION_SOCKET, timeout)
    try:
        connection.request(method, path, headers={"Content-Length": "0"})
        response = connection.getresponse()
        value = json.loads(response.read())
        if not isinstance(value, dict):
            raise TypeError("invalid staging response")
        return value
    finally:
        connection.close()


def _request_timeout_seconds(config: Path) -> float:
    """Resolve only the client-side socket bound.

    Operator separation deliberately denies the `butters-staging-ops` group read
    access to service-owned staging configuration, so an authorized operator
    running this CLI cannot open that file -- which previously made every
    validation command fail with PermissionError before it reached the socket.

    The value read here is nothing but a local timeout. The daemon loads and
    validates its own configuration at startup, and verifies the socket's type,
    owner, group, and mode before serving it, so falling back to the reviewed
    default grants the operator no access and relaxes no boundary. Any error
    other than permission still propagates.
    """

    try:
        settings = load_staging_settings(config)
    except PermissionError:
        return AgentIngressSettings().request_timeout_seconds
    validate_staging_settings(config, settings)
    return settings.agent_ingress.request_timeout_seconds


def main() -> int:
    options = parser().parse_args()
    config = Path(
        os.environ.get(
            "BUTTERS_STAGING_CONFIG", "/etc/butters-staging/assistant.toml"
        )
    )
    method, path = _selected_route(options)
    try:
        payload = _request(
            method,
            path,
            _request_timeout_seconds(config) + 12,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        print(json.dumps({"ok": False, "error": "staging_unavailable"}))
        return 1
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
