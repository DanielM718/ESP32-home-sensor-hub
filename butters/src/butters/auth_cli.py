"""Local-root-only passkey bootstrap and recovery CLI."""

from __future__ import annotations

import argparse
import os
import pwd
import sys
from pathlib import Path

from butters.assistant_config import load_assistant_settings
from butters.auth.store import AuthStateError, AuthStateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butters-passkey")
    parser.add_argument("--config", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "bootstrap", help="create one short-lived first-passkey authorization"
    )
    recovery = commands.add_parser(
        "recover", help="locally revoke all passkeys and clear elevation state"
    )
    recovery.add_argument("--confirm-local-recovery", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.geteuid() != 0:
        print("error: local root privileges are required", file=sys.stderr)
        return 2
    settings = load_assistant_settings(args.config)
    try:
        service_account = pwd.getpwnam("butters")
    except KeyError:
        print("error: the butters service account is not provisioned", file=sys.stderr)
        return 2
    # Root authorizes entry to this local-only CLI. Database work then runs as
    # the service identity so SQLite/WAL files are never left root-owned.
    os.setgroups([service_account.pw_gid])
    os.setgid(service_account.pw_gid)
    os.setuid(service_account.pw_uid)
    store = AuthStateStore(
        settings.web.state_dir / "security.sqlite3", settings.authentication
    )
    try:
        if args.command == "bootstrap":
            token, expires = store.create_bootstrap()
            # This is the only intentional display of the bootstrap secret. It
            # is written to the invoking local terminal, never a log or trace.
            print(f"bootstrap_token={token}")
            print(f"expires_at={expires}")
            return 0
        if not args.confirm_local_recovery:
            print("error: --confirm-local-recovery is required", file=sys.stderr)
            return 2
        store.local_recovery_revoke_all()
        print(
            "all passkey public records removed; create a new local bootstrap authorization"
        )
        return 0
    except (AuthStateError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
