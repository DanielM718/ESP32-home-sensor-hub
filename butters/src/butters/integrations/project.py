"""Narrow, fixed-command read-only inspection of this repository."""

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
        repository_root: Path,
        *,
        runner: Callable[..., object] = subprocess.run,
        max_output_bytes: int = 16384,
    ) -> None:
        self.repository_root = Path(repository_root).resolve()
        self.runner = runner
        self.max_output_bytes = max_output_bytes

    def inspect(self, view: str) -> dict[str, object]:
        command = self.COMMANDS.get(view)
        if command is None:
            raise IntegrationError("policy_denied", "project view is not allow-listed")
        if not (self.repository_root / ".git").exists():
            raise IntegrationError("unavailable", "configured repository is unavailable")
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
            raise IntegrationError("unavailable", "Git inspection is unavailable") from exc
        raw = bytes(getattr(completed, "stdout", b"") or b"")
        truncated = len(raw) > self.max_output_bytes
        output = sanitize_text(
            raw[: self.max_output_bytes].decode("utf-8", errors="replace"),
            max_bytes=self.max_output_bytes,
        )
        if int(getattr(completed, "returncode", 1)) != 0:
            raise IntegrationError("git_failed", "Git inspection failed safely")
        return {
            "view": view,
            "output": output.text.strip() or "clean/no output",
            "truncated": truncated or output.truncated,
        }
