"""One-time local credential enrollment: JSON on stdin, user-scoped DPAPI on disk.

Run as the intended Windows user. No credentials in argv, output, or source.
"""

import json
import os
from pathlib import Path
import sys

from .platform.win32 import dpapi


def main():
    credentials = json.loads(sys.stdin.buffer.read(4096))
    if (set(credentials) != {"token", "command_key"}
            or any(not isinstance(v, str) or len(bytes.fromhex(v)) != 32
                   for v in credentials.values())):
        raise SystemExit("invalid_credentials")
    local = Path(os.environ["LOCALAPPDATA"]) / "ButtersAgent"
    local.mkdir(exist_ok=True, parents=True)
    path = local / "credentials.dpapi"
    # Enrollment is deliberately create-only. Rotation requires explicit removal/backup.
    with path.open("xb") as handle:
        handle.write(dpapi(json.dumps(credentials).encode(), protect=True))
    print("Agent credentials provisioned with user-scoped DPAPI")


if __name__ == "__main__":
    main()
