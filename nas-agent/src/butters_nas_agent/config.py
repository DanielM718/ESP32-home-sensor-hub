"""Strict operator-owned configuration and separate secret loading."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import tomllib

HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    url: str
    agent_id: str
    spki_sha256: str
    truenas_url: str
    truenas_username: str
    truenas_ca_file: Path
    jellyfin_url: str
    jellyfin_health_path: str
    timeout_seconds: float
    heartbeat_seconds: float
    shutdown_enabled: bool
    health_file: Path


def _private_file(path: Path, label: str) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o077
    ):
        raise ValueError(f"unsafe_{label}")


def load_config(path: Path) -> AgentConfig:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & 0o022
    ):
        raise ValueError("unsafe_configuration")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"schema_version", "agent", "truenas", "jellyfin"}:
        raise ValueError("invalid_configuration")
    if data.get("schema_version") != 1:
        raise ValueError("invalid_configuration")
    agent = data.get("agent")
    truenas = data.get("truenas")
    jellyfin = data.get("jellyfin")
    if not all(type(item) is dict for item in (agent, truenas, jellyfin)):
        raise ValueError("invalid_configuration")
    assert (
        isinstance(agent, dict)
        and isinstance(truenas, dict)
        and isinstance(jellyfin, dict)
    )
    if (
        set(agent)
        != {
            "url",
            "agent_id",
            "spki_sha256",
            "heartbeat_seconds",
            "health_file",
        }
        or set(truenas)
        != {
            "url",
            "username",
            "ca_file",
            "timeout_seconds",
            "shutdown_enabled",
        }
        or set(jellyfin) != {"url", "health_path"}
    ):
        raise ValueError("invalid_configuration")
    config = AgentConfig(
        url=str(agent["url"]),
        agent_id=str(agent["agent_id"]),
        spki_sha256=str(agent["spki_sha256"]),
        heartbeat_seconds=float(agent["heartbeat_seconds"]),
        health_file=Path(str(agent["health_file"])),
        truenas_url=str(truenas["url"]),
        truenas_username=str(truenas["username"]),
        truenas_ca_file=Path(str(truenas["ca_file"])),
        timeout_seconds=float(truenas["timeout_seconds"]),
        shutdown_enabled=truenas["shutdown_enabled"] is True,
        jellyfin_url=str(jellyfin["url"]),
        jellyfin_health_path=str(jellyfin["health_path"]),
    )
    agent_url = urlsplit(config.url)
    nas_url = urlsplit(config.truenas_url)
    jf_url = urlsplit(config.jellyfin_url)
    valid = (
        config.agent_id == "nas-primary"
        and HEX_64.fullmatch(config.spki_sha256)
        and agent_url.scheme == "wss"
        and agent_url.path == "/nas-agent/v1/session"
        and not any(
            (
                agent_url.username,
                agent_url.password,
                agent_url.query,
                agent_url.fragment,
            )
        )
        and nas_url.scheme == "wss"
        and nas_url.path == "/api/current"
        and not any(
            (nas_url.username, nas_url.password, nas_url.query, nas_url.fragment)
        )
        and USERNAME.fullmatch(config.truenas_username)
        and config.truenas_ca_file.is_absolute()
        and jf_url.scheme in {"http", "https"}
        and bool(jf_url.hostname)
        and not any((jf_url.username, jf_url.password, jf_url.query, jf_url.fragment))
        and config.jellyfin_health_path.startswith("/")
        and "?" not in config.jellyfin_health_path
        and "#" not in config.jellyfin_health_path
        and 0.2 <= config.timeout_seconds <= 10
        and 5 <= config.heartbeat_seconds <= 30
        and config.health_file.is_absolute()
    )
    if not valid:
        raise ValueError("invalid_configuration")
    return config


def load_agent_credentials(path: Path) -> dict[str, str]:
    _private_file(path, "agent_credentials")
    value = json.loads(path.read_text(encoding="utf-8"))
    try:
        valid = (
            type(value) is dict
            and set(value) == {"token", "command_key"}
            and all(
                isinstance(item, str) and len(bytes.fromhex(item)) == 32
                for item in value.values()
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid_agent_credentials")
    return dict(value)


def load_api_key(path: Path, label: str = "truenas_api_key") -> str:
    _private_file(path, label)
    value = path.read_text(encoding="utf-8").strip()
    if not 16 <= len(value) <= 512 or any(ch.isspace() for ch in value):
        raise ValueError("invalid_truenas_api_key")
    return value
