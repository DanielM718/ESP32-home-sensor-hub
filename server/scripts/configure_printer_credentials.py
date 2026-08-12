"""Atomically create the root-controlled printer observer credential file.

The Home Assistant long-lived access token is read from a hidden terminal
prompt. Existing ha-bambulab cloud authentication and the device ID are copied
directly from Home Assistant storage without being printed or placed in argv,
the process environment, or shell history.
"""

from __future__ import annotations

import getpass
import grp
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

CONFIG_ENTRIES = Path("/opt/home-assistant/config/.storage/core.config_entries")
OUTPUT = Path("/etc/home-sensor/printer.env")
HOME_ASSISTANT_URL = "http://127.0.0.1:8123/api/"
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~-]+$")


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("run this credential installer as root")
    cloud_token, device_id = _bambu_credentials(CONFIG_ENTRIES)
    ha_token = getpass.getpass(
        "Home Assistant long-lived access token (input hidden): "
    ).strip()
    _validate_value("Home Assistant token", ha_token)
    _verify_home_assistant_token(ha_token)
    _write_environment(ha_token, cloud_token, device_id)
    print("Installed /etc/home-sensor/printer.env without displaying credentials.")
    return 0


def _bambu_credentials(path: Path) -> tuple[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit("could not read Home Assistant config entries") from exc
    entries = payload.get("data", {}).get("entries", [])
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("domain") == "bambu_lab"
        and entry.get("disabled_by") is None
    ]
    if len(matches) != 1:
        raise SystemExit("expected exactly one enabled ha-bambulab config entry")
    entry = matches[0]
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    cloud_token = str(options.get("auth_token", "")).strip()
    device_id = str(data.get("serial", "")).strip()
    _validate_value("Bambu Cloud token", cloud_token)
    _validate_value("Bambu device ID", device_id)
    return cloud_token, device_id


def _validate_value(label: str, value: str) -> None:
    if not value or not SAFE_VALUE.fullmatch(value):
        raise SystemExit(f"{label} is missing or has an unsafe environment-file shape")


def _verify_home_assistant_token(token: str) -> None:
    request = urllib.request.Request(
        HOME_ASSISTANT_URL,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "home-sensor-printer-credential-installer/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read(4097)
            if response.status != 200:
                raise SystemExit("Home Assistant rejected the supplied token")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise SystemExit("Home Assistant rejected the supplied token") from None
        raise SystemExit("Home Assistant token verification failed") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SystemExit(
            "Home Assistant was unavailable for token verification"
        ) from exc


def _write_environment(ha_token: str, cloud_token: str, device_id: str) -> None:
    OUTPUT.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    group_id = grp.getgrnam("home-sensor").gr_gid
    content = (
        f"HOME_ASSISTANT_TOKEN={ha_token}\n"
        f"BAMBU_CLOUD_TOKEN={cloud_token}\n"
        f"BAMBU_DEVICE_ID={device_id}\n"
    ).encode()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".printer.env.", dir=OUTPUT.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o640)
        os.fchown(descriptor, 0, group_id)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT)
        os.chmod(OUTPUT, 0o640)
        os.chown(OUTPUT, 0, group_id)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
