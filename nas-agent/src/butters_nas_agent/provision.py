"""Create one distinct NAS machine identity without printing its secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from pathlib import Path


def _create(path: Path, value: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def provision(directory: Path) -> dict[str, str]:
    """Create agent and Butters halves in a new mode-0700 directory."""

    directory = directory.resolve(strict=False)
    if not directory.is_absolute():
        raise ValueError("output_directory_must_be_absolute")
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    token = secrets.token_hex(32)
    command_key = secrets.token_hex(32)
    agent_path = directory / "agent-credentials.json"
    key_path = directory / "command.key"
    server_path = directory / "butters-nas-agent.toml"
    _create(
        agent_path,
        json.dumps(
            {"token": token, "command_key": command_key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )
    _create(key_path, (command_key + "\n").encode("ascii"))
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    _create(
        server_path,
        (
            "schema_version = 1\n"
            "protocol_version = 1\n"
            'agent_id = "nas-primary"\n'
            f'token_sha256 = "{digest}"\n'
            f'command_key_file = "{key_path}"\n'
        ).encode("ascii"),
    )
    return {
        "identity": "nas-primary",
        "agent_credentials": str(agent_path),
        "butters_configuration": str(server_path),
        "command_key": str(key_path),
        "token_sha256": digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    options = parser.parse_args()
    result = provision(options.output_dir)
    # The token and signing key are deliberately absent from stdout.
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
