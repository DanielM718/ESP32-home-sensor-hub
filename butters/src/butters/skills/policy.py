"""Explicit deny-by-default authorization for registered typed skills."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from butters.skills.model import ActionClass, SkillArguments, SkillError

Authorizer = Callable[[SkillArguments], None]


class PolicyValidator:
    def __init__(
        self,
        *,
        allowed_actions: frozenset[ActionClass] = frozenset({ActionClass.READ_ONLY}),
    ) -> None:
        self.allowed_actions = allowed_actions

    def authorize(
        self,
        *,
        skill_name: str,
        action_class: ActionClass,
        arguments: SkillArguments,
        authorizer: Authorizer,
    ) -> None:
        if action_class not in self.allowed_actions:
            raise SkillError(
                "policy_denied",
                f"skill {skill_name} has disallowed action class {action_class.value}",
            )
        try:
            authorizer(arguments)
        except SkillError:
            raise
        except (TypeError, ValueError) as exc:
            raise SkillError("policy_denied", str(exc)) from exc


def allow_arguments(_arguments: Any) -> None:
    return None
