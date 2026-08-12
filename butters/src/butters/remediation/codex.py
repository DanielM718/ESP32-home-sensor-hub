"""Safest current Codex CLI adapter, disabled by default."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from butters.assistant_config import RemediationSettings
from butters.diagnostics.sanitizer import sanitize_text
from butters.remediation.environment import minimal_codex_environment
from butters.remediation.jobs import APPROVED_AFFECTED_SERVICES, CodexJobFactory, RemediationPolicyError
from butters.remediation.model import (
    CodexJob,
    EngineeringRemediationRequest,
    EngineeringRemediationResult,
    EngineeringStatus,
    RemediationMode,
)


class CodexCliRemediator:
    def __init__(
        self,
        settings: RemediationSettings,
        *,
        factory: CodexJobFactory | None = None,
        runner: Callable[..., object] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.settings = settings
        self.factory = factory or CodexJobFactory(settings)
        self.runner = runner
        self.which = which

    def prepare(self, request: EngineeringRemediationRequest) -> CodexJob:
        return self.factory.create(request)

    def run(self, request: EngineeringRemediationRequest) -> EngineeringRemediationResult:
        try:
            job = self.prepare(request)
        except RemediationPolicyError as exc:
            return EngineeringRemediationResult(
                EngineeringStatus.DENIED,
                request.problem_statement,
                summary=str(exc),
                stopping_reason=exc.code,
            )
        if not self.settings.allow_codex_execution:
            return EngineeringRemediationResult(
                EngineeringStatus.MANUAL_LAUNCH_REQUIRED,
                request.problem_statement,
                base_commit=job.base_commit,
                summary="Codex execution is disabled; the bounded job is ready for manual review.",
                output_excerpt=job.prompt,
                stopping_reason="codex_execution_disabled",
            )
        if self.which("codex") is None:
            return self._failure(request, job, EngineeringStatus.UNAVAILABLE, "codex_unavailable")
        root = Path(job.repository_root)
        before = self._git_status(root)
        try:
            completed = self.runner(
                list(job.argv),
                input=job.prompt,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=job.timeout_seconds,
                check=False,
                env=minimal_codex_environment(os.environ),
            )
        except subprocess.TimeoutExpired:
            return self._failure(request, job, EngineeringStatus.TIMEOUT, "codex_timeout")
        except OSError:
            return self._failure(request, job, EngineeringStatus.UNAVAILABLE, "codex_unavailable")
        output = str(getattr(completed, "stdout", ""))
        stderr = str(getattr(completed, "stderr", ""))
        bounded = sanitize_text(output + ("\n" + stderr if stderr else ""), max_bytes=self.settings.max_output_bytes)
        after = self._git_status(root)
        if request.mode is RemediationMode.INSPECT and before != after:
            return self._failure(
                request,
                job,
                EngineeringStatus.FAILED,
                "inspect_modified_worktree",
                excerpt=bounded.text,
                truncated=bounded.truncated,
            )
        if int(getattr(completed, "returncode", 1)) != 0:
            return self._failure(
                request,
                job,
                EngineeringStatus.FAILED,
                "codex_failed",
                excerpt=bounded.text,
                truncated=bounded.truncated,
            )
        return self._parse_result(request, job, bounded.text, bounded.truncated)

    def _parse_result(
        self,
        request: EngineeringRemediationRequest,
        job: CodexJob,
        output: str,
        truncated: bool,
    ) -> EngineeringRemediationResult:
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return self._failure(request, job, EngineeringStatus.FAILED, "malformed_result", excerpt=output, truncated=truncated)
        required = {
            "status", "problem", "root_cause", "files_changed", "tests", "tests_passed",
            "deployment_required", "services_affected", "risk", "rollback_plan", "summary",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            return self._failure(request, job, EngineeringStatus.FAILED, "malformed_result", excerpt=output, truncated=truncated)
        if not _string_list(value.get("files_changed"), 128) or not _string_list(value.get("tests"), 64) or not _string_list(value.get("services_affected"), 16):
            return self._failure(request, job, EngineeringStatus.FAILED, "malformed_result", excerpt=output, truncated=truncated)
        files = tuple(str(item) for item in value["files_changed"])
        try:
            for path in files:
                self.factory.resolve_repository_path(path)
        except RemediationPolicyError:
            return self._failure(request, job, EngineeringStatus.FAILED, "result_path_escape", excerpt=output, truncated=truncated)
        services = tuple(str(item) for item in value["services_affected"])
        if not set(services) <= APPROVED_AFFECTED_SERVICES:
            return self._failure(request, job, EngineeringStatus.FAILED, "result_service_denied", excerpt=output, truncated=truncated)
        status = EngineeringStatus.PATCH_READY if request.mode is RemediationMode.PATCH else EngineeringStatus.INSPECTION_READY
        return EngineeringRemediationResult(
            status,
            str(value["problem"])[:2000],
            root_cause=_optional_string(value["root_cause"]),
            base_commit=job.base_commit,
            files_changed=files,
            tests=tuple(str(item) for item in value["tests"]),
            tests_passed=value["tests_passed"] if isinstance(value["tests_passed"], bool) else None,
            deployment_required=bool(value["deployment_required"]),
            services_affected=services,
            risk=_optional_string(value["risk"]),
            rollback_plan=_optional_string(value["rollback_plan"]),
            summary=str(value["summary"])[:4000],
            output_excerpt=output,
            truncated=truncated,
            stopping_reason="codex_complete",
        )

    def _git_status(self, root: Path) -> str:
        try:
            completed = self.runner(
                ["git", "status", "--porcelain=v1"], cwd=root, capture_output=True,
                text=True, timeout=10, check=False,
                env=minimal_codex_environment(os.environ),
            )
        except (OSError, subprocess.SubprocessError):
            return "<git-unavailable>"
        return str(getattr(completed, "stdout", ""))

    @staticmethod
    def _failure(
        request: EngineeringRemediationRequest,
        job: CodexJob,
        status: EngineeringStatus,
        reason: str,
        *,
        excerpt: str | None = None,
        truncated: bool = False,
    ) -> EngineeringRemediationResult:
        return EngineeringRemediationResult(
            status,
            request.problem_statement,
            base_commit=job.base_commit,
            summary="Codex engineering work did not complete; the diagnostic session remains valid.",
            output_excerpt=excerpt,
            truncated=truncated,
            stopping_reason=reason,
        )


def _string_list(value: object, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(isinstance(item, str) and len(item) <= 2000 for item in value)
    )


def _optional_string(value: object) -> str | None:
    return value[:4000] if isinstance(value, str) else None
