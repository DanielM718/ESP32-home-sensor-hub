"""Deployment-time custody of production-local approvals.

`install-beta1` calls this before it publishes a staged tree. Three jobs:

* `migrate`  - one-time: lift approvals a running machine already holds in
               `/opt/butters/config/assistant.toml` into the persistent
               overlay, using the running configuration as source of truth;
* `export`   - print the effective gate values for a given configuration;
* `verify`   - compare the gates a deployment would produce against the ones
               currently in force and fail if any would change.

`verify` runs as a preflight rather than a post-install check: the swap is two
atomic renames and cannot be meaningfully undone afterwards, so a deployment
that would move a security-sensitive gate is refused before anything is
published.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import tomllib

from butters.config_overlay import (
    GATE_KEYS,
    LocalConfigError,
    gate_values,
    load_overlay,
    local_differences,
    merge_overlay,
    render_overlay,
)

MIGRATION_HEADER = """Production-local administrative approvals for Butters.

Owned by this machine, not by the repository. `install-beta1` replaces
/opt/butters wholesale on every deployment; this file is never touched by it,
so an approval recorded here survives a release and a revocation is not undone
by one.

Only the approval keys butters.config_overlay declares may appear here. The
authentication and identity boundary is deliberately not overridable. A
malformed or over-permissive file stops the service rather than falling back
to shipped defaults, because several gates ship enabled.

Edit deliberately, then restart butters-web.service."""


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocalConfigError(f"configuration not found: {path}") from exc
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LocalConfigError(f"cannot read configuration: {path}") from exc


def effective_gates(shipped: Path, overlay: Path | None) -> dict[str, bool | None]:
    data = _read_toml(shipped)
    if overlay is not None:
        data = merge_overlay(data, load_overlay(overlay))
    return gate_values(data)


def command_export(args: argparse.Namespace) -> int:
    overlay = Path(args.overlay) if args.overlay else None
    print(json.dumps(effective_gates(Path(args.config), overlay), sort_keys=True))
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    """Record the approvals a running machine already holds.

    Nothing is inferred from hardware or service availability: the only input
    is the configuration the machine is currently running. A machine with no
    deployed tree yet is a fresh install and gets no overlay at all, so it
    starts from the shipped defaults.
    """

    target = Path(args.overlay)
    if target.exists():
        print(f"{target} already exists; leaving it unchanged.")
        return 0
    deployed_path = Path(args.deployed)
    if not deployed_path.exists():
        print("No deployed configuration to migrate; shipped defaults apply.")
        return 0
    differences = local_differences(_read_toml(deployed_path), _read_toml(Path(args.shipped)))
    if not differences:
        print("Deployed configuration matches shipped defaults; no overlay needed.")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_overlay(differences, header=MIGRATION_HEADER), encoding="utf-8")
    flat = sorted(effective_gates(deployed_path, None).items())
    recorded = [key for key, _ in flat if _dotted_in(differences, key)]
    print(f"Recorded {len(recorded)} production-local approval(s) in {target}:")
    for key in recorded:
        print(f"  {key}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    """Refuse a deployment that would change a security-sensitive gate."""

    overlay = Path(args.overlay) if args.overlay else None
    before = effective_gates(Path(args.before), overlay)
    after = effective_gates(Path(args.after), overlay)
    changed = {
        key: (before[key], after[key]) for key in GATE_KEYS if before[key] != after[key]
    }
    if not changed:
        print(f"Deployment preserves all {len(GATE_KEYS)} declared approvals.")
        return 0
    print(
        "Refusing to deploy: it would change production-local approvals that "
        "this deployment does not own.",
        file=sys.stderr,
    )
    for key, (old, new) in sorted(changed.items()):
        print(f"  {key}: {old} -> {new}", file=sys.stderr)
    print(
        "\nRecord the intended value in the production-local overlay, or state "
        "the change deliberately with --allow-gate-changes.",
        file=sys.stderr,
    )
    return 4


def _dotted_in(tree: dict[str, Any], dotted: str) -> bool:
    node: Any = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="butters.deployment_gates")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export", help="print effective gate values as JSON")
    export.add_argument("--config", required=True)
    export.add_argument("--overlay")
    export.set_defaults(handler=command_export)

    migrate = sub.add_parser("migrate", help="create the overlay from a running machine")
    migrate.add_argument("--deployed", required=True)
    migrate.add_argument("--shipped", required=True)
    migrate.add_argument("--overlay", required=True)
    migrate.set_defaults(handler=command_migrate)

    verify = sub.add_parser("verify", help="refuse a deployment that moves a gate")
    verify.add_argument("--before", required=True)
    verify.add_argument("--after", required=True)
    verify.add_argument("--overlay")
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except LocalConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 5


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
