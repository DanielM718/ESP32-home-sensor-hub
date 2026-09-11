"""Planner provider contract plus safe disabled and deterministic providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from butters.planner.model import PlannerError, PlannerRequest


class PlannerProvider(Protocol):
    """Untrusted proposal source. Its result must always pass PlannerValidator."""

    name: str

    @property
    def available(self) -> bool: ...

    def plan(self, request: PlannerRequest) -> object: ...


class DisabledPlannerProvider:
    name = "disabled"
    available = False

    def plan(self, request: PlannerRequest) -> object:
        del request
        raise PlannerError(
            "planner_unavailable",
            "Conversational planning is not configured on this deployment.",
        )


class DeterministicPlannerProvider:
    """Small fake provider for boundary tests; it performs no I/O or execution."""

    name = "deterministic_fake"
    available = True

    def __init__(self, responses: Mapping[str, object] | None = None) -> None:
        self.responses = dict(responses or {})

    def plan(self, request: PlannerRequest) -> object:
        if request.user_text in self.responses:
            response = self.responses[request.user_text]
            if isinstance(response, Exception):
                raise response
            return response
        normalized = " ".join(request.user_text.lower().strip().split())
        match = {
            "check my desktop": ("get_desktop_status", {"machine": "desktop"}),
            "wake my desktop": ("wake_desktop", {"machine": "desktop"}),
            "open git bash": ("desktop.app.launch", {"app": "git_bash"}),
            "open parsec": ("desktop.app.launch", {"app": "parsec"}),
            "shut down my desktop": (
                "shutdown_desktop",
                {"machine": "desktop"},
            ),
        }.get(normalized)
        if match is None:
            raise PlannerError(
                "clarification_required",
                "Please ask for desktop status, wake, Git Bash, Parsec, or shutdown.",
            )
        action_id, parameters = match
        if action_id not in {item.action_id for item in request.actions}:
            raise PlannerError(
                "planner_unavailable", "The requested registered action is unavailable."
            )
        return {
            "summary": normalized.capitalize() + ".",
            "rationale": "Use the existing registered desktop action.",
            "requires_confirmation": False,
            "steps": [{"action_id": action_id, "parameters": parameters}],
        }
