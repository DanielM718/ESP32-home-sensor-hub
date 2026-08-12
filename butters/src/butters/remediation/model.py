"""Typed requests and results for the distinct engineering remediation tier."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class EngineeringClassification(str, Enum):
    OPERATIONAL_DIAGNOSTIC = "operational_diagnostic"
    CONFIGURATION_PROBLEM = "configuration_problem"
    SOFTWARE_DEFECT = "software_defect"
    DEPLOYMENT_PROBLEM = "deployment_problem"
    UNKNOWN = "unknown"


class RemediationMode(str, Enum):
    INSPECT = "inspect"
    PATCH = "patch"
    DEPLOY = "deploy"


class EngineeringOperation(str, Enum):
    READ_REPOSITORY = "read_repository"
    READ_GIT_HISTORY = "read_git_history"
    RUN_APPROVED_TESTS = "run_approved_tests"
    EDIT_REPOSITORY = "edit_repository"
    PRODUCE_PATCH = "produce_patch"


class EngineeringStatus(str, Enum):
    INSPECTION_READY = "inspection_ready"
    PATCH_READY = "patch_ready"
    MANUAL_LAUNCH_REQUIRED = "manual_launch_required"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EngineeringRemediationRequest:
    problem_statement: str
    classification: EngineeringClassification
    affected_subsystem: str
    mode: RemediationMode = RemediationMode.INSPECT
    diagnostic_findings: tuple[str, ...] = ()
    evidence_references: tuple[str, ...] = ()
    repository_alias: str = "home_sensor"
    deployment_target: str | None = None
    required_test_ids: tuple[str, ...] = ()
    production_mutation_permission: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CodexJob:
    mode: RemediationMode
    repository_root: str
    base_commit: str
    allowed_operations: tuple[EngineeringOperation, ...]
    required_tests: tuple[str, ...]
    prompt: str
    argv: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class EngineeringRemediationResult:
    status: EngineeringStatus
    problem: str
    root_cause: str | None = None
    base_commit: str | None = None
    files_changed: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    tests_passed: bool | None = None
    deployment_required: bool = False
    services_affected: tuple[str, ...] = ()
    risk: str | None = None
    rollback_plan: str | None = None
    summary: str = ""
    output_excerpt: str | None = None
    truncated: bool = False
    stopping_reason: str | None = None


class EngineeringRemediator(ABC):
    @abstractmethod
    def prepare(self, request: EngineeringRemediationRequest) -> CodexJob:
        """Build a locally authorized engineering job without executing it."""

    @abstractmethod
    def run(self, request: EngineeringRemediationRequest) -> EngineeringRemediationResult:
        """Run a supported job, or return a typed safe failure."""
