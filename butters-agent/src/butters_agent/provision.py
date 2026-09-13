"""Enroll credentials from stdin into a profile-specific DPAPI store."""

import argparse
import json
import os
import sys
from pathlib import Path

import tomllib

from .platform.win32 import dpapi
from .profile import local_data_root, profile_name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    options = parser.parse_args()
    config: dict[str, object] = {}
    if options.config is not None:
        config = tomllib.loads(options.config.read_text(encoding="utf-8-sig"))
    try:
        profile_name(config)
    except ValueError:
        raise SystemExit("invalid_configuration") from None
    credentials = json.loads(sys.stdin.buffer.read(4096))
    try:
        valid = (
            type(credentials) is dict
            and set(credentials) == {"token", "command_key"}
            and all(
                isinstance(value, str) and len(bytes.fromhex(value)) == 32
                for value in credentials.values()
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise SystemExit("invalid_credentials")
    local = local_data_root(os.environ["LOCALAPPDATA"], config)
    local.mkdir(exist_ok=True, parents=True)
    path = local / "credentials.dpapi"
    # Enrollment is create-only. Rotation requires explicit removal or backup.
    with path.open("xb") as handle:
        payload = json.dumps(credentials).encode()
        handle.write(dpapi(payload, protect=True))
    print(
        "Agent credentials provisioned with user-scoped DPAPI for "
        f"the {profile_name(config)} profile"
    )


if __name__ == "__main__":
    main()
