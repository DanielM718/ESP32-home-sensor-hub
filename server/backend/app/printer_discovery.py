"""Credential-safe, read-only Home Assistant Bambu entity discovery."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.printer_adapter import discover_bambu_entities

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def run() -> int:
    token = os.environ.get("HOME_ASSISTANT_TOKEN", "").strip()
    if not token:
        raise SystemExit("HOME_ASSISTANT_TOKEN is required and is never printed")
    base_url = os.environ.get("HOME_ASSISTANT_URL", "http://127.0.0.1:8123").rstrip("/")
    _validate_url(base_url)
    config = _get_json(base_url, "/api/config", token)
    states = _get_json(base_url, "/api/states", token)
    if not isinstance(config, Mapping) or not isinstance(states, list):
        raise SystemExit("Home Assistant returned an unexpected response shape")
    components = config.get("components", [])
    component_names = {
        str(item).lower() for item in components if isinstance(item, str)
    }
    bambu_components = sorted(name for name in component_names if "bambu" in name)
    payload = {
        "home_assistant_version": config.get("version"),
        "hacs_component_loaded": "hacs" in component_names,
        "bambu_components_loaded": bambu_components,
        "candidates": discover_bambu_entities(
            [item for item in states if isinstance(item, Mapping)]
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _get_json(base_url: str, path: str, token: str) -> Any:
    request = Request(
        f"{base_url}{path}",
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise SystemExit("Home Assistant authentication was denied") from None
        raise SystemExit(f"Home Assistant returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise SystemExit("Home Assistant is unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise SystemExit("Home Assistant response exceeded the size limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SystemExit("Home Assistant returned malformed JSON") from None


def _validate_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("HOME_ASSISTANT_URL must be an HTTP(S) URL")
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise SystemExit("plain HTTP discovery is restricted to loopback")


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
