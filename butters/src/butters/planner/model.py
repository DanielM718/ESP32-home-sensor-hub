"""Strict, bounded data contracts for conversational planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class PlannerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PlannerCatalogAction:
    action_id: str
    description: str
    input_schema: dict[str, object]
    action_class: str
    authentication: str
    confirmation_required: bool

    def safe_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "description": self.description,
            "input_schema": self.input_schema,
            "action_class": self.action_class,
            "authentication": self.authentication,
            "confirmation_required": self.confirmation_required,
        }


@dataclass(frozen=True, slots=True)
class PlannerRequest:
    user_text: str
    actions: tuple[PlannerCatalogAction, ...]
    current_state: dict[str, object]
    conversation: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class PlannerStep:
    action_id: str
    parameters: dict[str, object]

    def safe_dict(self) -> dict[str, object]:
        return {"action_id": self.action_id, "parameters": self.parameters}


@dataclass(frozen=True, slots=True)
class PlannerPlan:
    summary: str
    rationale: str
    steps: tuple[PlannerStep, ...]
    # This field is always overwritten by deterministic registry policy during
    # validation. A provider's opinion is retained nowhere executable.
    requires_confirmation: bool
    required_authentication: Literal["none", "elevated", "fresh"]

    def safe_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "rationale": self.rationale,
            "steps": [step.safe_dict() for step in self.steps],
            "requires_confirmation": self.requires_confirmation,
            "required_authentication": self.required_authentication,
        }
