"""Uvicorn entry point for the persistent Butters web service."""

from __future__ import annotations

import os

import uvicorn

from butters.assistant_config import load_assistant_settings


def main() -> None:
    settings = load_assistant_settings()
    uvicorn.run(
        "butters.web.app:create_app",
        factory=True,
        host=settings.web.host,
        port=settings.web.port,
        workers=1,
        ws_max_size=settings.browser_audio.max_chunk_bytes,
        timeout_keep_alive=10,
        log_level=os.getenv("BUTTERS_LOG_LEVEL", "info").lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
