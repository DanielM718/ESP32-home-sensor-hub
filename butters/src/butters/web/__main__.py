"""Uvicorn entry point for the persistent Butters web service.

This is the only supported way to run the web service. `scripts/butters-web`
and `butters-web.service` both invoke `python -m butters.web`, so every
launcher shares the options built here rather than restating them, and a new
launcher that bypasses this module would lose the proxy-header invariant
below. `tests/test_runtime_config_consistency.py` holds those launchers to it.
"""

from __future__ import annotations

import os

import uvicorn

from butters.assistant_config import AssistantSettings, load_assistant_settings

APPLICATION = "butters.web.app:create_app"


def server_options(settings: AssistantSettings) -> dict[str, object]:
    """Every uvicorn option the service runs with, in one place.

    Host and port come from the settings passed in, which are loaded through
    the same resolver `create_app()` uses, so the listener and the application
    are always configured by the same file.
    """

    return {
        "factory": True,
        "host": settings.web.host,
        "port": settings.web.port,
        "workers": 1,
        "ws_max_size": settings.browser_audio.max_chunk_bytes,
        "timeout_keep_alive": 10,
        "log_level": os.getenv("BUTTERS_LOG_LEVEL", "info").lower(),
        "access_log": False,
        # Preserve the TCP peer established by Tailscale Serve. Butters trusts
        # proxy-supplied identity only after independently checking that peer,
        # so letting uvicorn rewrite the peer from X-Forwarded-For would let a
        # tailnet caller present itself as the loopback proxy. Uvicorn's own
        # default is True, which is why this is stated rather than assumed.
        "proxy_headers": False,
    }


def main() -> None:
    uvicorn.run(APPLICATION, **server_options(load_assistant_settings()))


if __name__ == "__main__":
    main()
