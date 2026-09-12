"""Enroll credentials from stdin into user-scoped Windows DPAPI storage."""

import json
import os
import sys
from pathlib import Path

from .platform.win32 import dpapi


def main():
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
    local = Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent"
    local.mkdir(exist_ok=True, parents=True)
    path = local / "credentials.dpapi"
    # Enrollment is create-only. Rotation requires explicit removal or backup.
    with path.open("xb") as handle:
        payload = json.dumps(credentials).encode()
        handle.write(dpapi(payload, protect=True))
    print("Agent credentials provisioned with user-scoped DPAPI")


if __name__ == "__main__":
    main()
