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
    if set(raw) != {"broker", "desktop", "nas", "operations"}:
        raise ValueError("broker configuration sections are invalid")
    broker = raw["broker"]
    desktop = raw["desktop"]
    nas = raw["nas"]
    operations = raw["operations"]
    if set(broker) != {"service_user"}:
        raise ValueError("broker configuration fields are invalid")
    if set(desktop) != {"host", "user", "mac", "broadcast", "key"}:
        raise ValueError("desktop broker fields are invalid")
    if set(nas) != {"mac", "broadcast"}:
        raise ValueError("NAS broker fields are invalid")
    expected_operations = {item.value for item in BrokerOperation}
    if set(operations) != expected_operations or not all(
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
        BrokerOperation.DESKTOP_ENTER_REMOTE,
        BrokerOperation.DESKTOP_RESTORE_LOCAL,
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
    return uid, FixedBrokerConfig(
        str(desktop["host"]),
        str(desktop["user"]),
        str(desktop["mac"]),
        str(desktop["broadcast"]),
        key,
        str(nas["mac"]),
        str(nas["broadcast"]),
        enabled_operations,
    )


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
        server = BrokerServer(
            FixedBrokerOperations(configuration).handlers(),
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
