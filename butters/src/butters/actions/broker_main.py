"""Systemd socket-activated entry point for the privileged action broker."""

from __future__ import annotations

import argparse
import logging
import os
import pwd
import socket
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

import tomllib

from butters.actions.broker import (
    BrokerOperation,
    BrokerServer,
    FixedBrokerConfig,
    FixedBrokerOperations,
)


def _configuration(path: Path) -> tuple[int, FixedBrokerConfig]:
    _require_root_private(path, "broker configuration")
    with path.open("rb") as source:
        raw = tomllib.load(source)
    if set(raw) != {"broker", "desktop", "home_assistant", "nas", "operations"}:
        raise ValueError("broker configuration sections are invalid")
    broker = raw["broker"]
    desktop = raw["desktop"]
    home_assistant = raw["home_assistant"]
    nas = raw["nas"]
    operations = raw["operations"]
    if set(broker) != {"service_user"}:
        raise ValueError("broker configuration fields are invalid")
    if set(desktop) != {"host", "user", "mac", "broadcast", "key"}:
        raise ValueError("desktop broker fields are invalid")
    if set(home_assistant) != {"url"}:
        raise ValueError("Home Assistant broker fields are invalid")
    # The shutdown transport fields are optional so that a configuration written
    # before they existed still parses. An unknown field is still refused, so
    # this loosens nothing except the flag day: adding a privileged operation to
    # the enum must not make every already-deployed broker configuration invalid
    # and take the whole privileged surface down until a root file is edited.
    if not {"mac", "broadcast"} <= set(nas) <= {
        "mac",
        "broadcast",
        "api_url",
        "api_key",
    }:
        raise ValueError("NAS broker fields are invalid")
    expected_operations = {item.value for item in BrokerOperation}
    # Same reasoning, and it stays fail-closed: an operation the file does not
    # mention is disabled, never enabled. Only an unknown or non-boolean gate is
    # an error.
    if not set(operations) <= expected_operations or not all(
        isinstance(value, bool) for value in operations.values()
    ):
        raise ValueError("broker operation gates are invalid")
    enabled_operations = frozenset(
        BrokerOperation(name) for name, enabled in operations.items() if enabled
    )
    uid = pwd.getpwnam(str(broker["service_user"])).pw_uid
    key = Path(str(desktop["key"]))
    if not key.is_absolute():
        raise ValueError("desktop credential path must be absolute")
    desktop_ssh_operations = {
        BrokerOperation.DESKTOP_PARSEC_STATUS,
        BrokerOperation.DESKTOP_PARSEC_ENSURE,
        BrokerOperation.DESKTOP_PARSEC_RESTART,
        BrokerOperation.DESKTOP_LOCK,
        BrokerOperation.DESKTOP_SLEEP,
        BrokerOperation.DESKTOP_RESTART,
        BrokerOperation.DESKTOP_SHUTDOWN,
    }
    if enabled_operations & desktop_ssh_operations:
        _require_root_private(key, "desktop credential")
        # Fail closed rather than fall back to trust-on-first-use: the pinned
        # host key must already be provisioned beside the credential.
        _require_root_private(key.parent / "known_hosts", "desktop known_hosts")
    home_assistant_url = str(home_assistant["url"]).rstrip("/")
    parsed = urlparse(home_assistant_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Home Assistant URL must be HTTP(S)")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("plain HTTP Home Assistant access is restricted to loopback")
    nas_api_url = str(nas.get("api_url", "")).rstrip("/")
    nas_api_key_path = str(nas.get("api_key", ""))
    if nas_api_url:
        parsed_nas = urlparse(nas_api_url)
        if parsed_nas.scheme != "https" or not parsed_nas.hostname:
            raise ValueError("NAS API URL must be HTTPS")
        if not nas_api_key_path:
            raise ValueError("NAS API access requires a credential file")
    if nas_api_key_path:
        if not Path(nas_api_key_path).is_absolute():
            raise ValueError("NAS API credential path must be absolute")
        _require_root_private(Path(nas_api_key_path), "NAS API credential")
    if BrokerOperation.NAS_SHUTDOWN in enabled_operations and not (
        nas_api_url and nas_api_key_path
    ):
        raise ValueError("enabled NAS shutdown operation requires a fixed transport")
    return uid, FixedBrokerConfig(
        desktop_host=str(desktop["host"]),
        desktop_user=str(desktop["user"]),
        desktop_mac=str(desktop["mac"]),
        desktop_broadcast=str(desktop["broadcast"]),
        desktop_key=key,
        nas_mac=str(nas["mac"]),
        nas_broadcast=str(nas["broadcast"]),
        nas_api_url=nas_api_url,
        enabled_operations=enabled_operations,
        home_assistant_url=home_assistant_url,
    )


def _nas_api_key(config_path: Path) -> str:
    """Read the root-owned TrueNAS credential without exposing its path later.

    The broker runs as root and the service user never sees this file, so the
    secret exists only in the broker process. An unreadable or empty file leaves
    the shutdown handler unregistered rather than half-configured.
    """

    with config_path.open("rb") as source:
        raw = tomllib.load(source)
    key_path = str(raw.get("nas", {}).get("api_key", ""))
    if not key_path:
        return ""
    value = Path(key_path).read_text(encoding="utf-8").strip()
    if len(value) > 4096:
        raise ValueError("NAS API credential is too large")
    return value


def _require_root_private(path: Path, label: str) -> None:
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_mode & 0o077
    ):
        raise ValueError(f"{label} must be a root-owned private regular file")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="butters-action-broker")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/butters/action-broker.toml"),
    )
    args = parser.parse_args(argv)
    # The unit has no log file of its own; stderr is what journald captures, and
    # the broker's audit line is deliberately the only record it emits per
    # connection. Timestamps and unit identity come from journald.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        expected_uid, configuration = _configuration(args.config)
        nas_api_key = _nas_api_key(args.config)
        server = BrokerServer(
            FixedBrokerOperations(
                configuration,
                home_assistant_token=os.environ.get("HOME_ASSISTANT_TOKEN", ""),
                nas_api_key=nas_api_key,
            ).handlers(),
            expected_uid=expected_uid,
        )
        if (
            os.getenv("LISTEN_PID") != str(os.getpid())
            or os.getenv("LISTEN_FDS") != "1"
        ):
            raise ValueError("exactly one systemd-activated listener is required")
        listener = socket.socket(fileno=3)
        while True:
            connection, _address = listener.accept()
            try:
                server.handle(connection)
            finally:
                connection.close()
    except (OSError, ValueError, KeyError) as exc:
        print(f"broker error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
