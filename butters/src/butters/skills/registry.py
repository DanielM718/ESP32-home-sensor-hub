"""Typed skill lookup, validation, policy authorization, and execution."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from butters.integrations.model import IntegrationError
from butters.skills.model import (
    ActionClass,
    SkillArguments,
    SkillAudience,
    SkillError,
    SkillExecution,
    SkillFailure,
    SkillResult,
)
from butters.skills.policy import Authorizer, PolicyValidator

ArgumentParser = Callable[[Mapping[str, object]], SkillArguments]
Implementation = Callable[[SkillArguments], SkillResult]


@dataclass(frozen=True, slots=True)
class SkillSpec:
    name: str
    description: str
    action_class: ActionClass
    parse_arguments: ArgumentParser
    authorize: Authorizer
    implementation: Implementation
    timeout_seconds: float
    version: str = "1.0.0"
    category: str = "general"
    input_schema: dict[str, object] = field(default_factory=dict)
    result_description: str = "Structured read-only result."
    permission_summary: tuple[str, ...] = ("read_only",)
    positive_examples: tuple[str, ...] = ()
    negative_examples: tuple[str, ...] = ()
    source_reference: str = "butters.skills.implementations"
    validation_status: str = "covered_by_registry_tests"
    audience: SkillAudience = SkillAudience.NORMAL

    def metadata(self, *, enabled: bool = True) -> dict[str, object]:
        """Return safe declarative metadata; never return executable callables."""

        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "category": self.category,
            "action_class": self.action_class.value,
            "audience": self.audience.value,
            "enabled": enabled,
            "input_schema": self.input_schema,
            "result_description": self.result_description,
            "permission_summary": list(self.permission_summary),
            "timeout_seconds": self.timeout_seconds,
            "positive_examples": list(self.positive_examples),
            "negative_examples": list(self.negative_examples),
            "source_reference": self.source_reference,
            "validation_status": self.validation_status,
        }


class SkillRegistry:
    def __init__(self, policy: PolicyValidator | None = None) -> None:
        self._policy = policy or PolicyValidator()
        self._skills: dict[str, SkillSpec] = {}
        self._disabled: set[str] = set()

    @property
    def skills(self) -> tuple[SkillSpec, ...]:
        return tuple(self._skills.values())

    def register(self, spec: SkillSpec) -> None:
        if spec.name in self._skills:
            raise ValueError(f"duplicate skill: {spec.name}")
        if spec.timeout_seconds <= 0:
            raise ValueError("skill timeout must be positive")
        self._skills[spec.name] = spec

    def get(self, skill_name: str) -> SkillSpec | None:
        return self._skills.get(skill_name)

    def is_enabled(self, skill_name: str) -> bool:
        return skill_name in self._skills and skill_name not in self._disabled

    def set_enabled(self, skill_name: str, enabled: bool) -> None:
        spec = self._skills.get(skill_name)
        if spec is None:
            raise SkillError("unknown_skill", "skill is not registered")
        if spec.action_class is not ActionClass.READ_ONLY:
            raise SkillError("policy_denied", "only read-only skills may be toggled")
        if enabled:
            self._disabled.discard(skill_name)
        else:
            self._disabled.add(skill_name)

    def metadata(self, *, administrator: bool = True) -> tuple[dict[str, object], ...]:
        return tuple(
            spec.metadata(enabled=self.is_enabled(spec.name))
            for spec in self._skills.values()
            if administrator or spec.audience is SkillAudience.NORMAL
        )

    def requires_administrator(self, skill_name: str) -> bool:
        spec = self._skills.get(skill_name)
        return spec is not None and spec.audience is SkillAudience.ADMINISTRATOR

    def execute(
        self,
        skill_name: str,
        arguments: Mapping[str, object],
        *,
        administrator: bool = False,
    ) -> SkillExecution:
        started = time.perf_counter()
        spec = self._skills.get(skill_name)
        if spec is None:
            return SkillExecution(
                skill_name,
                None,
                time.perf_counter() - started,
                failure=SkillFailure("unknown_skill", "skill is not registered"),
                arguments=dict(arguments),
            )
        if not self.is_enabled(skill_name):
            return SkillExecution(
                skill_name,
                spec.action_class,
                time.perf_counter() - started,
                failure=SkillFailure("skill_disabled", "skill is disabled"),
                arguments=dict(arguments),
            )
        if spec.audience is SkillAudience.ADMINISTRATOR and not administrator:
            return SkillExecution(
                skill_name,
                spec.action_class,
                time.perf_counter() - started,
                failure=SkillFailure(
                    "administrator_required",
                    "this observation requires administrator authorization",
                ),
                arguments=dict(arguments),
            )
        try:
            parsed = spec.parse_arguments(arguments)
            self._policy.authorize(
                skill_name=spec.name,
                action_class=spec.action_class,
                arguments=parsed,
                authorizer=spec.authorize,
            )
            result = spec.implementation(parsed)
            elapsed = time.perf_counter() - started
            if elapsed > spec.timeout_seconds:
                raise SkillError("timeout", f"{skill_name} exceeded its deadline")
            return SkillExecution(
                skill_name,
                spec.action_class,
                elapsed,
                result=result,
                arguments=dict(arguments),
            )
        except (SkillError, IntegrationError) as exc:
            return SkillExecution(
                skill_name,
                spec.action_class,
                time.perf_counter() - started,
                failure=SkillFailure(exc.code, str(exc)),
                arguments=dict(arguments),
            )
        # A skill implementation is a trust boundary. Convert unexpected adapter
        # failures into a non-sensitive error instead of leaking details to a
        # future model or voice response.
        except Exception:  # noqa: BLE001
            return SkillExecution(
                skill_name,
                spec.action_class,
                time.perf_counter() - started,
                failure=SkillFailure(
                    "internal_error", "the read-only skill could not complete"
                ),
                arguments=dict(arguments),
            )

    def validate_proposal(
        self,
        skill_name: str,
        arguments: Mapping[str, object],
        *,
        administrator: bool = False,
    ) -> SkillFailure | None:
        """Apply typed parsing and policy without calling an integration adapter."""
        spec = self._skills.get(skill_name)
        if spec is None:
            return SkillFailure("unknown_skill", "skill is not registered")
        if not self.is_enabled(skill_name):
            return SkillFailure("skill_disabled", "skill is disabled")
        if spec.audience is SkillAudience.ADMINISTRATOR and not administrator:
            return SkillFailure(
                "administrator_required",
                "this observation requires administrator authorization",
            )
        try:
            parsed = spec.parse_arguments(arguments)
            self._policy.authorize(
                skill_name=spec.name,
                action_class=spec.action_class,
                arguments=parsed,
                authorizer=spec.authorize,
            )
        except SkillError as exc:
            return SkillFailure(exc.code, str(exc))
        except Exception:  # noqa: BLE001
            return SkillFailure("policy_denied", "proposal validation failed")
        return None


def strict_arguments(
    values: Mapping[str, object],
    *,
    required: frozenset[str] = frozenset(),
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(values)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise SkillError(
            "invalid_arguments", f"missing arguments: {', '.join(sorted(missing))}"
        )
    if unexpected:
        raise SkillError(
            "invalid_arguments",
            f"unexpected arguments: {', '.join(sorted(unexpected))}",
        )


def required_string(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SkillError("invalid_arguments", f"{key} must be a non-empty string")
    return value.strip()


def required_string_tuple(
    values: Mapping[str, object], key: str, *, maximum: int = 8
) -> tuple[str, ...]:
    """Parse a bounded, de-duplicated, order-preserving list of identifiers."""

    value = values.get(key)
    if not isinstance(value, (list, tuple)) or not value:
        raise SkillError("invalid_arguments", f"{key} must be a non-empty list")
    if len(value) > maximum:
        raise SkillError(
            "invalid_arguments", f"{key} accepts at most {maximum} entries"
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SkillError(
                "invalid_arguments", f"{key} entries must be non-empty strings"
            )
        cleaned = item.strip()
        if cleaned not in items:
            items.append(cleaned)
    return tuple(items)


def optional_string(values: Mapping[str, object], key: str) -> str | None:
    value: Any = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SkillError("invalid_arguments", f"{key} must be a string or null")
    return value.strip()
