"""Strict operator-owned configuration and separate secret loading."""

from __future__ import annotations

import json
import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import tomllib

HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
INTERFACE = re.compile(r"[a-zA-Z0-9_.:-]{1,32}\Z")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    url: str
    agent_id: str
    spki_sha256: str
    truenas_url: str
    truenas_username: str
    truenas_shutdown_username: str | None
    truenas_spki_sha256: str
    jellyfin_url: str
    jellyfin_health_path: str
    jellyfin_session_monitoring_enabled: bool
    local_networks: tuple[str, ...]
    remote_networks: tuple[str, ...]
    network_monitoring_enabled: bool
    physical_interface: str
    tailscale_metrics_url: str
    sample_interval_seconds: float
    smoothing_alpha: float
    maximum_sample_interval_seconds: float
    effective_capacity_mbps: float
    safe_streaming_budget_mbps: float
    reserve_mbps: float
    minimum_stream_mbps: float
    maximum_stream_mbps: float
    stream_stability_seconds: float
    minimum_change_mbps: float
    policy_cooldown_seconds: float
    policy_mode: str
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
    if not {"agent", "truenas", "jellyfin"}.issubset(data) or set(data) - {
        "schema_version",
        "agent",
        "truenas",
        "jellyfin",
        "network",
        "bandwidth",
    }:
        raise ValueError("invalid_configuration")
    if data.get("schema_version") != 1:
        raise ValueError("invalid_configuration")
    agent = data.get("agent")
    truenas = data.get("truenas")
    jellyfin = data.get("jellyfin")
    network = data.get("network", {})
    bandwidth = data.get("bandwidth", {})
    if not all(
        type(item) is dict for item in (agent, truenas, jellyfin, network, bandwidth)
    ):
        raise ValueError("invalid_configuration")
    assert (
        isinstance(agent, dict)
        and isinstance(truenas, dict)
        and isinstance(jellyfin, dict)
        and isinstance(network, dict)
        and isinstance(bandwidth, dict)
    )
    truenas_fields = {
        "url",
        "username",
        "spki_sha256",
        "timeout_seconds",
        "shutdown_enabled",
    }
    shutdown_enabled = truenas.get("shutdown_enabled") is True
    expected_truenas_fields = (
        truenas_fields | {"shutdown_username"} if shutdown_enabled else truenas_fields
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
        or set(truenas) != expected_truenas_fields
        or set(jellyfin)
        - {
            "url",
            "health_path",
            "session_monitoring_enabled",
            "local_networks",
            "remote_networks",
        }
        or not {"url", "health_path"}.issubset(jellyfin)
        or set(network)
        - {
            "enabled",
            "physical_interface",
            "tailscale_metrics_url",
            "sample_interval_seconds",
            "smoothing_alpha",
            "maximum_sample_interval_seconds",
        }
        or set(bandwidth)
        - {
            "effective_capacity_mbps",
            "safe_streaming_budget_mbps",
            "reserve_mbps",
            "minimum_stream_mbps",
            "maximum_stream_mbps",
            "stream_stability_seconds",
            "minimum_change_mbps",
            "policy_cooldown_seconds",
            "policy_mode",
        }
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
        truenas_shutdown_username=(
            str(truenas["shutdown_username"]) if shutdown_enabled else None
        ),
        truenas_spki_sha256=str(truenas["spki_sha256"]),
        timeout_seconds=float(truenas["timeout_seconds"]),
        shutdown_enabled=shutdown_enabled,
        jellyfin_url=str(jellyfin["url"]),
        jellyfin_health_path=str(jellyfin["health_path"]),
        jellyfin_session_monitoring_enabled=bool(
            jellyfin.get("session_monitoring_enabled", False)
        ),
        local_networks=tuple(str(item) for item in jellyfin.get("local_networks", [])),
        remote_networks=tuple(
            str(item)
            for item in jellyfin.get(
                "remote_networks", ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
            )
        ),
        network_monitoring_enabled=bool(network.get("enabled", False)),
        physical_interface=str(network.get("physical_interface", "")),
        tailscale_metrics_url=str(
            network.get("tailscale_metrics_url", "http://100.100.100.100/metrics")
        ),
        sample_interval_seconds=float(network.get("sample_interval_seconds", 5.0)),
        smoothing_alpha=float(network.get("smoothing_alpha", 0.35)),
        maximum_sample_interval_seconds=float(
            network.get("maximum_sample_interval_seconds", 30.0)
        ),
        effective_capacity_mbps=float(bandwidth.get("effective_capacity_mbps", 30.0)),
        safe_streaming_budget_mbps=float(
            bandwidth.get("safe_streaming_budget_mbps", 24.0)
        ),
        reserve_mbps=float(bandwidth.get("reserve_mbps", 6.0)),
        minimum_stream_mbps=float(bandwidth.get("minimum_stream_mbps", 3.0)),
        maximum_stream_mbps=float(bandwidth.get("maximum_stream_mbps", 24.0)),
        stream_stability_seconds=float(bandwidth.get("stream_stability_seconds", 15.0)),
        minimum_change_mbps=float(bandwidth.get("minimum_change_mbps", 1.0)),
        policy_cooldown_seconds=float(bandwidth.get("policy_cooldown_seconds", 30.0)),
        policy_mode=str(bandwidth.get("policy_mode", "observe")),
    )
    agent_url = urlsplit(config.url)
    nas_url = urlsplit(config.truenas_url)
    jf_url = urlsplit(config.jellyfin_url)
    metrics_url = urlsplit(config.tailscale_metrics_url)
    try:
        networks_valid = all(
            str(ipaddress.ip_network(item, strict=True)) == item
            for item in (*config.local_networks, *config.remote_networks)
        )
    except ValueError:
        networks_valid = False
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
        and (
            not config.shutdown_enabled
            or (
                config.truenas_shutdown_username is not None
                and USERNAME.fullmatch(config.truenas_shutdown_username)
                and config.truenas_shutdown_username != config.truenas_username
            )
        )
        and HEX_64.fullmatch(config.truenas_spki_sha256)
        and jf_url.scheme in {"http", "https"}
        and bool(jf_url.hostname)
        and not any((jf_url.username, jf_url.password, jf_url.query, jf_url.fragment))
        and config.jellyfin_health_path.startswith("/")
        and "?" not in config.jellyfin_health_path
        and "#" not in config.jellyfin_health_path
        and networks_valid
        and (
            not config.jellyfin_session_monitoring_enabled
            or bool(config.local_networks)
        )
        and (
            not config.network_monitoring_enabled
            or bool(INTERFACE.fullmatch(config.physical_interface))
        )
        and metrics_url.scheme == "http"
        and metrics_url.hostname == "100.100.100.100"
        and metrics_url.path == "/metrics"
        and not any(
            (
                metrics_url.username,
                metrics_url.password,
                metrics_url.port,
                metrics_url.query,
                metrics_url.fragment,
            )
        )
        and 0 < config.smoothing_alpha <= 1
        and 2 <= config.sample_interval_seconds <= 30
        and 5 <= config.maximum_sample_interval_seconds <= 120
        and 1 <= config.effective_capacity_mbps <= 10_000
        and 0 < config.safe_streaming_budget_mbps <= config.effective_capacity_mbps
        and 0 <= config.reserve_mbps < config.effective_capacity_mbps
        and config.safe_streaming_budget_mbps
        <= config.effective_capacity_mbps - config.reserve_mbps + 1e-9
        and 0.5 <= config.minimum_stream_mbps <= config.maximum_stream_mbps
        and config.maximum_stream_mbps <= config.safe_streaming_budget_mbps
        and 0 <= config.stream_stability_seconds <= 300
        and 0.1 <= config.minimum_change_mbps <= config.maximum_stream_mbps
        and 0 <= config.policy_cooldown_seconds <= 600
        and config.policy_mode in {"off", "observe", "dry_run"}
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
