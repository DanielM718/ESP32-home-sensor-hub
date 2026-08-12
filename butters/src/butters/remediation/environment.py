"""Explicit secret-free process environment for local Codex workers."""

from __future__ import annotations

import os
from collections.abc import Mapping


# Codex CLI authentication is independent of provider API keys. These values
# are sufficient to locate the binary, locale, user home, and Codex auth files.
# Everything else is denied rather than inherited.
CODEX_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "USER",
        "LOGNAME",
        "SHELL",
        "HOME",
        "CODEX_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)

_SECRET_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "COOKIE",
    "MQTT",
    "INFLUX",
    "HOME_ASSISTANT",
    "OPENAI",
    "ANTHROPIC",
    "AZURE",
    "GOOGLE",
    "ADMIN_AUTH",
    "ADMIN_IDENT",
    "DATABASE",
)


def minimal_codex_environment(
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an allow-listed environment with a second secret-name guard."""

    source = os.environ if parent is None else parent
    environment = {
        name: value
        for name, value in source.items()
        if name in CODEX_ENV_ALLOWLIST
        and not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }
    # Predictable subprocess behavior without importing any parent deployment
    # variables. Codex still uses its own existing login under HOME/CODEX_HOME.
    environment.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    environment.setdefault("LANG", "C.UTF-8")
    return environment


def sensitive_environment_names(
    parent: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return names that make same-process Codex execution unsafe via /proc."""

    source = os.environ if parent is None else parent
    return tuple(
        sorted(
            name
            for name, value in source.items()
            if value and any(marker in name.upper() for marker in _SECRET_MARKERS)
        )
    )
