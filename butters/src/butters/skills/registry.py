"""Typed skill lookup, validation, policy authorization, and execution."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from butters.integrations.model import IntegrationError
from butters.skills.model import (
    ActionClass,
    SkillArguments,
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


class SkillRegistry:
    def __init__(self, policy: PolicyValidator | None = None) -> None:
        self._policy = policy or PolicyValidator()
        self._skills: dict[str, SkillSpec] = {}

    @property
    def skills(self) -> tuple[SkillSpec, ...]:
        return tuple(self._skills.values())

    def register(self, spec: SkillSpec) -> None:
        if spec.name in self._skills:
            raise ValueError(f"duplicate skill: {spec.name}")
        if spec.timeout_seconds <= 0:
            raise ValueError("skill timeout must be positive")
        self._skills[spec.name] = spec

    def execute(
        self, skill_name: str, arguments: Mapping[str, object]
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


def optional_string(values: Mapping[str, object], key: str) -> str | None:
    value: Any = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SkillError("invalid_arguments", f"{key} must be a string or null")
    return value.strip()
