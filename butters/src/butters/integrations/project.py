"""Narrow, fixed-command read-only inspection of a configured repository.

An ordinary deployed Butters daemon has no repository at all: the installed tree
under `/opt/butters` is not a checkout, and the service deliberately has no read
access to a developer's private home directory. Repository inspection is
therefore an opt-in deployment decision, and every unavailable case is reported
as a typed failure rather than an exception.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from butters.diagnostics.sanitizer import sanitize_text
from butters.integrations.model import IntegrationError


class ProjectInspectionAdapter:
    COMMANDS: dict[str, tuple[str, ...]] = {
        "status": ("git", "status", "--short", "--branch"),
        "branch": ("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "base_commit": ("git", "rev-parse", "HEAD"),
        "recent_commits": ("git", "log", "--oneline", "-10"),
        "diff_summary": ("git", "diff", "--stat", "--"),
    }

    def __init__(
        self,
        repository_root: Path | None,
        *,
        runner: Callable[..., object] = subprocess.run,
        max_output_bytes: int = 16384,
    ) -> None:
        self.repository_root = self._resolve(repository_root)
        self.runner = runner
        self.max_output_bytes = max_output_bytes

    @property
    def available(self) -> bool:
        return self.repository_root is not None and self._is_repository()

    def inspect(self, view: str) -> dict[str, object]:
        command = self.COMMANDS.get(view)
        if command is None:
            raise IntegrationError("policy_denied", "project view is not allow-listed")
        if self.repository_root is None:
            raise IntegrationError(
                "repository_unavailable",
                "no readable repository is configured for this deployment",
            )
        if not self._is_repository():
            raise IntegrationError(
                "repository_unavailable",
                "the configured repository is missing or not readable",
            )
        try:
            completed = self.runner(
                list(command),
                cwd=self.repository_root,
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrationError(
                "repository_unavailable", "Git inspection is unavailable"
            ) from exc
        raw = bytes(getattr(completed, "stdout", b"") or b"")
        truncated = len(raw) > self.max_output_bytes
        output = sanitize_text(
            raw[: self.max_output_bytes].decode("utf-8", errors="replace"),
            max_bytes=self.max_output_bytes,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            # A foreign-owned checkout fails Git's ownership check here; that is
            # a deployment condition, not an application error.
            raise IntegrationError("git_failed", "Git inspection failed safely")
        return {
            "view": view,
            "output": output.text.strip() or "clean/no output",
            "truncated": truncated or output.truncated,
        }

    def _is_repository(self) -> bool:
        assert self.repository_root is not None
        try:
            return (self.repository_root / ".git").exists()
        except OSError:
            return False

    @staticmethod
    def _resolve(repository_root: Path | None) -> Path | None:
        if repository_root is None:
            return None
        try:
            return Path(repository_root).resolve()
        except OSError:
            return None
