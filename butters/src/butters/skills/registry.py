"""Typed skill lookup, validation, policy authorization, and execution."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass
from queue import Empty, Queue
from typing import Any

from butters.integrations.model import IntegrationError
from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
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
_CURRENT_CANCEL_EVENT: ContextVar[threading.Event | None] = ContextVar(
    "butters_skill_cancel_event", default=None
)
_CURRENT_JOB_ID: ContextVar[str | None] = ContextVar(
    "butters_skill_job_id", default=None
)


def current_cancel_event() -> threading.Event | None:
    return _CURRENT_CANCEL_EVENT.get()


def current_job_id() -> str | None:
    return _CURRENT_JOB_ID.get()


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
    output_schema: dict[str, object] = field(default_factory=dict)
    explicit_intent_required: bool = False
    confirmation_required: bool = False
    side_effects: str = "none"
    max_result_bytes: int = 8192
    authentication: AuthenticationLevel = AuthenticationLevel.NONE
    local_console_allowed: bool = False
    configured: bool = True
    available: bool = True
    unavailable_reason: str | None = None

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
            "output_schema": self.output_schema,
            "explicit_intent_required": self.explicit_intent_required,
            "confirmation_required": self.confirmation_required,
            "side_effects": self.side_effects,
            "max_result_bytes": self.max_result_bytes,
            "authentication": self.authentication.value,
            "local_console_allowed": self.local_console_allowed,
            "configured": self.configured,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
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
        if (
            spec.action_class is ActionClass.ACTION
            and spec.authentication is AuthenticationLevel.NONE
        ):
            raise ValueError("action skills require an explicit authentication level")
        if (
            spec.local_console_allowed
            and spec.authentication is not AuthenticationLevel.ELEVATED
        ):
            raise ValueError(
                "local-console alternatives apply only to elevated actions"
            )
        self._skills[spec.name] = spec

    def get(self, skill_name: str) -> SkillSpec | None:
        return self._skills.get(skill_name)

    def is_enabled(self, skill_name: str) -> bool:
        return skill_name in self._skills and skill_name not in self._disabled

    def set_enabled(self, skill_name: str, enabled: bool) -> None:
        spec = self._skills.get(skill_name)
        if spec is None:
            raise SkillError("unknown_skill", "skill is not registered")
        if spec.action_class is ActionClass.ACTION:
            raise SkillError(
                "policy_denied", "action skills may not be toggled at runtime"
            )
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
        action_authorization: ActionAuthorization | None = None,
        authentication_context: AuthenticationContext | None = None,
        session_id: str = "",
        identity: str = "",
        action_digest: str | None = None,
        cancel_event: threading.Event | None = None,
        job_id: str | None = None,
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
        if not spec.available:
            return SkillExecution(
                skill_name,
                spec.action_class,
                time.perf_counter() - started,
                failure=SkillFailure(
                    "capability_unavailable",
                    spec.unavailable_reason or "capability is unavailable",
                ),
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
                action_authorization=action_authorization,
                explicit_intent_required=spec.explicit_intent_required,
                confirmation_required=spec.confirmation_required,
                authentication=spec.authentication,
                local_console_allowed=spec.local_console_allowed,
                authentication_context=authentication_context,
                session_id=session_id,
                identity=identity,
                action_digest=action_digest,
            )
            if cancel_event is not None and cancel_event.is_set():
                raise SkillError("cancelled", "skill execution was cancelled")
            if spec.version.startswith("2."):
                result = _run_bounded(
                    spec.implementation,
                    parsed,
                    spec.timeout_seconds,
                    cancel_event,
                    job_id,
                )
            else:
                cancel_token = _CURRENT_CANCEL_EVENT.set(cancel_event)
                job_token = _CURRENT_JOB_ID.set(job_id)
                try:
                    result = spec.implementation(parsed)
                finally:
                    _CURRENT_JOB_ID.reset(job_token)
                    _CURRENT_CANCEL_EVENT.reset(cancel_token)
            encoded_result = json.dumps(
                asdict(result) if is_dataclass(result) else result,
                default=str,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            if len(encoded_result) > spec.max_result_bytes:
                raise SkillError("result_too_large", "skill result exceeded its limit")
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
        action_authorization: ActionAuthorization | None = None,
        authentication_context: AuthenticationContext | None = None,
        session_id: str = "",
        identity: str = "",
        action_digest: str | None = None,
    ) -> SkillFailure | None:
        """Apply typed parsing and policy without calling an integration adapter."""
        spec = self._skills.get(skill_name)
        if spec is None:
            return SkillFailure("unknown_skill", "skill is not registered")
        if not self.is_enabled(skill_name):
            return SkillFailure("skill_disabled", "skill is disabled")
        if not spec.available:
            return SkillFailure(
                "capability_unavailable",
                spec.unavailable_reason or "capability is unavailable",
            )
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
                action_authorization=action_authorization,
                explicit_intent_required=spec.explicit_intent_required,
                confirmation_required=spec.confirmation_required,
                authentication=spec.authentication,
                local_console_allowed=spec.local_console_allowed,
                authentication_context=authentication_context,
                session_id=session_id,
                identity=identity,
                action_digest=action_digest,
            )
        except SkillError as exc:
            return SkillFailure(exc.code, str(exc))
        except Exception:  # noqa: BLE001
            return SkillFailure("policy_denied", "proposal validation failed")
        return None

    def validate_action_intent(
        self,
        skill_name: str,
        arguments: Mapping[str, object],
    ) -> tuple[dict[str, object] | None, SkillFailure | None]:
        """Validate and canonicalize an ACTION before authentication.

        This deliberately does not authorize execution. It exists only so an
        exact immutable plan can be frozen before a human WebAuthn or local
        console ceremony occurs.
        """

        spec = self._skills.get(skill_name)
        if spec is None:
            return None, SkillFailure("unknown_skill", "skill is not registered")
        if (
            not self.is_enabled(skill_name)
            or spec.action_class is not ActionClass.ACTION
        ):
            return None, SkillFailure("policy_denied", "skill is not an enabled action")
        if not spec.available:
            return None, SkillFailure(
                "capability_unavailable",
                spec.unavailable_reason or "capability is unavailable",
            )
        try:
            parsed = spec.parse_arguments(arguments)
            spec.authorize(parsed)
            canonical = asdict(parsed) if is_dataclass(parsed) else dict(arguments)
            encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode()) > 4096:
                raise SkillError(
                    "invalid_arguments", "action arguments exceed their limit"
                )
            return canonical, None
        except (SkillError, IntegrationError, TypeError, ValueError) as exc:
            code = getattr(exc, "code", "invalid_arguments")
            return None, SkillFailure(code, str(exc))
        except Exception:  # noqa: BLE001
            return None, SkillFailure(
                "policy_denied", "action intent validation failed"
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


def _run_bounded(
    implementation: Implementation,
    arguments: SkillArguments,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
    job_id: str | None,
) -> SkillResult:
    event = cancel_event or threading.Event()
    outcome: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def invoke() -> None:
        cancel_token = _CURRENT_CANCEL_EVENT.set(event)
        job_token = _CURRENT_JOB_ID.set(job_id)
        try:
            outcome.put((True, implementation(arguments)))
        except BaseException as exc:  # noqa: BLE001
            outcome.put((False, exc))
        finally:
            _CURRENT_JOB_ID.reset(job_token)
            _CURRENT_CANCEL_EVENT.reset(cancel_token)

    worker = threading.Thread(
        target=invoke,
        name="butters-bounded-skill",
        daemon=True,
    )
    worker.start()
    try:
        ok, value = outcome.get(timeout=timeout_seconds)
    except Empty as exc:
        event.set()
        raise SkillError("timeout", "skill exceeded its deadline") from exc
    if not ok:
        assert isinstance(value, BaseException)
        raise value
    return value  # type: ignore[return-value]
