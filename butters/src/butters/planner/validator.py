"""Deterministic validation between an untrusted planner and registered skills."""

from __future__ import annotations

import json
from collections.abc import Mapping

from butters.planner.model import (
    PlannerCatalogAction,
    PlannerError,
    PlannerPlan,
    PlannerStep,
)
from butters.skills.model import ActionClass, AuthenticationLevel, SkillAudience
from butters.skills.registry import SkillRegistry

MAX_PLAN_BYTES = 8192


class PlannerValidator:
    def __init__(self, registry: SkillRegistry, *, max_actions: int = 3) -> None:
        if not 1 <= max_actions <= 4:
            raise ValueError("planner max_actions must be one to four")
        self.registry = registry
        self.max_actions = max_actions

    def catalog(
        self,
        action_ids: frozenset[str],
        *,
        parameter_enums: Mapping[str, Mapping[str, tuple[str, ...]]] | None = None,
    ) -> tuple[PlannerCatalogAction, ...]:
        result = []
        parameter_enums = parameter_enums or {}
        for action_id in sorted(action_ids):
            spec = self.registry.get(action_id)
            if (
                spec is None
                or not self.registry.is_enabled(action_id)
                or not spec.available
            ):
                continue
            schema = dict(spec.input_schema)
            if action_id in parameter_enums:
                properties = {
                    key: dict(value) if isinstance(value, Mapping) else value
                    for key, value in dict(schema.get("properties", {})).items()
                }
                for parameter, values in parameter_enums[action_id].items():
                    existing = properties.get(parameter)
                    if isinstance(existing, Mapping):
                        properties[parameter] = {**existing, "enum": list(values)}
                schema["properties"] = properties
            result.append(
                PlannerCatalogAction(
                    action_id,
                    spec.description,
                    schema,
                    spec.action_class.value,
                    spec.authentication.value,
                    spec.confirmation_required,
                )
            )
        return tuple(result)

    def validate(
        self,
        value: object,
        *,
        catalog: tuple[PlannerCatalogAction, ...],
        administrator: bool,
    ) -> PlannerPlan:
        raw = self._mapping(value, "plan")
        if set(raw) != {"summary", "rationale", "requires_confirmation", "steps"}:
            raise PlannerError("malformed_plan", "Planner output fields are invalid.")
        summary = self._text(raw["summary"], "summary", 500)
        rationale = self._text(raw["rationale"], "rationale", 1000)
        if type(raw["requires_confirmation"]) is not bool:
            raise PlannerError(
                "malformed_plan", "Planner confirmation value must be boolean."
            )
        raw_steps = raw["steps"]
        if (
            not isinstance(raw_steps, list)
            or not 1 <= len(raw_steps) <= self.max_actions
        ):
            raise PlannerError(
                "plan_limit",
                f"Planner output must contain one to {self.max_actions} actions.",
            )
        allowed = {item.action_id for item in catalog}
        steps: list[PlannerStep] = []
        auth = AuthenticationLevel.NONE
        confirmation = False
        for raw_step in raw_steps:
            item = self._mapping(raw_step, "step")
            if set(item) != {"action_id", "parameters"}:
                raise PlannerError("malformed_plan", "Planner step fields are invalid.")
            action_id = item["action_id"]
            parameters = item["parameters"]
            if not isinstance(action_id, str) or action_id not in allowed:
                raise PlannerError(
                    "unknown_action", "Planner selected an unknown action."
                )
            if not isinstance(parameters, Mapping):
                raise PlannerError(
                    "malformed_plan", "Action parameters must be an object."
                )
            spec = self.registry.get(action_id)
            assert spec is not None
            catalog_entry = next(
                entry for entry in catalog if entry.action_id == action_id
            )
            properties = catalog_entry.input_schema.get("properties", {})
            if isinstance(properties, Mapping):
                for parameter, parameter_value in parameters.items():
                    parameter_schema = properties.get(parameter)
                    if (
                        isinstance(parameter_schema, Mapping)
                        and "enum" in parameter_schema
                    ):
                        enum = parameter_schema["enum"]
                        if not isinstance(enum, list) or parameter_value not in enum:
                            raise PlannerError(
                                "invalid_arguments",
                                "Action parameter is not in the planner allow-list.",
                            )
            if spec.audience is SkillAudience.ADMINISTRATOR and not administrator:
                raise PlannerError(
                    "administrator_required",
                    "The selected action requires an administrator.",
                )
            if spec.action_class is ActionClass.ACTION:
                canonical, failure = self.registry.validate_action_intent(
                    action_id, parameters
                )
            else:
                failure = self.registry.validate_proposal(
                    action_id, parameters, administrator=administrator
                )
                canonical = (
                    None
                    if failure
                    else self.registry.canonical_arguments(action_id, parameters)
                )
            if failure is not None or canonical is None:
                raise PlannerError(
                    failure.code if failure else "invalid_arguments",
                    failure.message if failure else "Action arguments are invalid.",
                )
            steps.append(PlannerStep(action_id, canonical))
            confirmation = confirmation or spec.confirmation_required
            if spec.authentication is AuthenticationLevel.FRESH:
                auth = AuthenticationLevel.FRESH
            elif (
                spec.authentication is AuthenticationLevel.ELEVATED
                and auth is AuthenticationLevel.NONE
            ):
                auth = AuthenticationLevel.ELEVATED
        if len(steps) > 1 and any(
            self.registry.get(step.action_id).action_class is not ActionClass.ACTION
            for step in steps
        ):
            raise PlannerError(
                "unsupported_composition",
                "This planner slice supports sequences of registered actions only.",
            )
        try:
            encoded = json.dumps(
                [step.safe_dict() for step in steps],
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise PlannerError(
                "malformed_plan", "Planner output is not JSON-safe."
            ) from exc
        if len(encoded) > MAX_PLAN_BYTES:
            raise PlannerError(
                "plan_too_large", "Planner output exceeds its size limit."
            )
        return PlannerPlan(
            summary,
            rationale,
            tuple(steps),
            confirmation,
            auth.value,  # type: ignore[arg-type]
        )

    @staticmethod
    def _mapping(value: object, label: str) -> Mapping[str, object]:
        if type(value) is not dict:
            raise PlannerError("malformed_plan", f"Planner {label} must be an object.")
        return value

    @staticmethod
    def _text(value: object, label: str, limit: int) -> str:
        if not isinstance(value, str):
            raise PlannerError("malformed_plan", f"Planner {label} must be text.")
        clean = " ".join(value.replace("\x00", "").split())
        if not clean or len(clean) > limit:
            raise PlannerError("malformed_plan", f"Planner {label} is invalid.")
        return clean
