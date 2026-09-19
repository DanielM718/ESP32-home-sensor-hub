"""Persistent production-local configuration, kept outside the deployable tree.

`/opt/butters` is application state: `install-beta1` replaces it wholesale on
every deployment. `/opt/butters/config/assistant.toml` therefore cannot own an
administrative approval, because publishing identical application code would
silently rewrite it to whatever the repository happens to ship. That is not a
theoretical risk: deploying baf2203 reverted three production approvals -
`nas_agent_ingress.enabled`, `actions.nas_shutdown.enabled`, and
`actions.nas_shutdown.configured` - and disconnected the NAS Agent.

Ownership is therefore split explicitly:

    /opt/butters/config/assistant.toml   shipped defaults, replaceable
              +
    /etc/butters/assistant.local.toml    production-local approvals, persistent
              +
    /etc/butters/butters.conf            machine values, via the environment
              ↓
          effective configuration

This is not a general configuration editor. Only the keys in `GATE_KEYS` may
appear in the overlay, each is type-checked, and anything else is refused. The
overlay can grant or revoke a *capability approval*; it deliberately cannot
touch the authentication and identity boundary (`authentication.enabled`,
`web.development_mode`, `web.trusted_tailscale_proxy`, `web.admin_identities`),
because weakening those must stay a reviewed repository change.

Failure is closed. A malformed, unreadable, or over-permissive overlay raises
rather than falling back to shipped defaults: several gates ship `true`
(`desktop.shutdown_enabled`, `actions.nas.enabled`), so silently ignoring the
overlay could *grant* a capability the operator had locally revoked.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import tomllib

from butters.config import ConfigError

# The one environment variable that activates the overlay. The units ship it,
# so it is version-controlled rather than hand-set, and a developer checkout
# or a test never picks up a production machine's approvals by accident.
LOCAL_CONFIG_ENVIRONMENT = "BUTTERS_LOCAL_CONFIG"
DEFAULT_LOCAL_CONFIG_PATH = Path("/etc/butters/assistant.local.toml")
# The overlay grants and revokes capabilities, so only root may write it.
REQUIRED_OWNER_UID = 0

# Every production-local administrative approval, as a dotted TOML path.
#
# Membership here is the ownership statement: a key in this tuple is owned by
# the machine and survives deployment; a key absent from it is owned by the
# repository and is expected to change with a release. Only booleans qualify -
# an approval is a yes or a no, and admitting free-form values here would
# recreate the arbitrary-config-editor this design refuses.
GATE_KEYS: tuple[str, ...] = (
    # Separate-machine ingress gates.
    "agent_ingress.enabled",
    "nas_agent_ingress.enabled",
    # Desktop capability approvals. `desktop.shutdown_enabled` ships true, so
    # a local revocation here must outlive a deployment.
    "desktop.enabled",
    "desktop.wake_enabled",
    "desktop.headless_enabled",
    "desktop.monitors_enabled",
    "desktop.parsec_status_enabled",
    "desktop.parsec_ensure_enabled",
    "desktop.parsec_restart_enabled",
    "desktop.lock_enabled",
    "desktop.sleep_enabled",
    "desktop.restart_enabled",
    "desktop.shutdown_enabled",
    # Host power and service control.
    "actions.host_restart_butters_enabled",
    "actions.host_reboot_enabled",
    "actions.host_shutdown_enabled",
    # Per-device action gates. `enabled`, `configured`, and
    # `local_console_allowed` stay three separate facts; the overlay carries
    # each one rather than collapsing them into a single "authorized".
    *(
        f"actions.{device}.{flag}"
        for device in ("nas", "nas_shutdown", "heater", "dehumidifier", "ventilation")
        for flag in ("enabled", "configured", "local_console_allowed")
    ),
    # Paid and cloud approvals. These ship false and must stay false unless a
    # machine deliberately says otherwise.
    "cloud.enabled",
    "cloud.allow_paid_calls",
    "providers.allow_paid_stt",
    "providers.allow_paid_tts",
    # Other capability approvals.
    "broker.enabled",
    "portal.enabled",
    "planner.enabled",
    "llm.enabled",
    "diagnostics.enabled",
    "remediation.allow_codex_execution",
)

# Refused outright, with a specific message, so an operator who tries to move
# the authentication boundary into machine-local state is told why instead of
# getting a generic "unknown key".
BOUNDARY_KEYS: frozenset[str] = frozenset(
    {
        "authentication.enabled",
        "web.development_mode",
        "web.trusted_tailscale_proxy",
        "web.admin_identities",
        "web.allowed_origins",
    }
)


class LocalConfigError(ConfigError):
    """Raised when the production-local overlay cannot be trusted."""


def local_config_path(environment: dict[str, str] | None = None) -> Path | None:
    """The overlay path, or None when this process does not use one."""

    source = os.environ if environment is None else environment
    configured = source.get(LOCAL_CONFIG_ENVIRONMENT, "").strip()
    return Path(configured).expanduser() if configured else None


def load_overlay(path: Path) -> dict[str, Any]:
    """Read and validate the overlay. A missing file is an empty overlay."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # A machine that has never recorded a local approval is not an error;
        # it simply runs the shipped defaults.
        return {}
    except (OSError, PermissionError) as exc:
        raise LocalConfigError(
            f"production-local configuration cannot be read: {path}"
        ) from exc
    _require_protected(path)
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LocalConfigError(
            f"production-local configuration is not valid TOML: {path}"
        ) from exc
    return validated_overlay(data, source=str(path))


def validated_overlay(data: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Refuse any key outside the declared ownership boundary."""

    if not isinstance(data, dict):
        raise LocalConfigError(f"{source} must be a TOML table")
    allowed = set(GATE_KEYS)
    for dotted, value in sorted(_flatten(data)):
        if dotted in BOUNDARY_KEYS:
            raise LocalConfigError(
                f"{source} may not set {dotted}: the authentication and identity "
                "boundary is owned by the reviewed repository configuration"
            )
        if dotted not in allowed:
            raise LocalConfigError(
                f"{source} may not set {dotted}: only declared production-local "
                "approvals may be overridden"
            )
        if not isinstance(value, bool):
            raise LocalConfigError(
                f"{source} value for {dotted} must be true or false"
            )
    return data


def merge_overlay(
    shipped: dict[str, Any], overlay: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge the overlay over the shipped tables, overlay winning."""

    merged = dict(shipped)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = merge_overlay(existing, value)
        else:
            merged[key] = value
    return merged


def gate_values(data: dict[str, Any]) -> dict[str, bool | None]:
    """Every declared gate's effective value, for comparison across a deploy.

    `None` means the key is absent from this configuration, which is itself a
    difference worth reporting rather than silently reading as false.
    """

    flat = dict(_flatten(data))
    return {
        key: flat[key] if isinstance(flat.get(key), bool) else None
        for key in GATE_KEYS
    }


def local_differences(
    deployed: dict[str, Any], shipped: dict[str, Any]
) -> dict[str, Any]:
    """Gates where a running machine already disagrees with shipped defaults.

    This is the migration input. It reads approval from the configuration the
    machine is actually running, never from whether a service happens to be
    reachable: an agent being connected is evidence about the network, not
    evidence that an administrator approved the capability.
    """

    running = gate_values(deployed)
    defaults = gate_values(shipped)
    overlay: dict[str, Any] = {}
    for key in GATE_KEYS:
        current = running.get(key)
        if current is None or current == defaults.get(key):
            continue
        _assign(overlay, key, current)
    return overlay


def render_overlay(overlay: dict[str, Any], *, header: str) -> str:
    """Emit a deterministic, reviewable TOML document."""

    lines = [line if line.startswith("#") else f"# {line}" for line in header.splitlines()]
    lines.append("")
    for section in sorted(overlay):
        body = overlay[section]
        if not isinstance(body, dict):
            continue
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        if scalars:
            lines.append(f"[{section}]")
            lines.extend(f"{k} = {str(scalars[k]).lower()}" for k in sorted(scalars))
            lines.append("")
        for name in sorted(tables):
            lines.append(f"[{section}.{name}]")
            lines.extend(
                f"{k} = {str(tables[name][k]).lower()}" for k in sorted(tables[name])
            )
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _require_protected(path: Path) -> None:
    """The overlay decides capability approvals, so the service user must not
    be able to write it. Root owns it; `butters` only reads it.

    `REQUIRED_OWNER_UID` is a module constant rather than a literal so the test
    suite can exercise the merge behaviour as an ordinary user while the
    production default stays root-only. A test that lowers it is asserting
    about merging, not about ownership; the ownership rule itself is asserted
    separately against the unmodified default.
    """

    info = path.stat()
    if info.st_uid != REQUIRED_OWNER_UID:
        raise LocalConfigError(
            f"production-local configuration must be owned by uid "
            f"{REQUIRED_OWNER_UID}: {path}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_IROTH):
        raise LocalConfigError(
            f"production-local configuration must not be group-writable or "
            f"world-readable: {path}"
        )


def _flatten(node: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            items.extend(_flatten(value, dotted + "."))
        else:
            items.append((dotted, value))
    return items


def _assign(target: dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = target
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
