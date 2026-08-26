"""Explicit deny-by-default authorization for registered typed skills."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
    SkillArguments,
    SkillError,
)

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
        action_authorization: ActionAuthorization | None = None,
        explicit_intent_required: bool = False,
        confirmation_required: bool = False,
        authentication: AuthenticationLevel = AuthenticationLevel.NONE,
        local_console_allowed: bool = False,
        authentication_context: AuthenticationContext | None = None,
        session_id: str = "",
        identity: str = "",
        action_digest: str | None = None,
    ) -> None:
        if action_class not in self.allowed_actions:
            raise SkillError(
                "policy_denied",
                f"skill {skill_name} has disallowed action class {action_class.value}",
            )
        if action_class is ActionClass.ACTION:
            if action_authorization is None or not action_authorization.permits(
                skill_name
            ):
                raise SkillError(
                    "action_confirmation_required",
                    "the exact action requires explicit user authorization",
                )
            if explicit_intent_required and action_authorization.source not in {
                "direct_user_request",
                "confirmed_user_request",
            }:
                raise SkillError(
                    "action_confirmation_required",
                    "the action was not directly requested by the user",
                )
            if confirmation_required and not action_authorization.confirmed:
                raise SkillError(
                    "action_confirmation_required",
                    "the action requires confirmation",
                )
            self._authorize_authentication(
                authentication,
                local_console_allowed,
                authentication_context,
                session_id=session_id,
                identity=identity,
                action_digest=action_digest,
            )
        try:
            authorizer(arguments)
        except SkillError:
            raise
        except (TypeError, ValueError) as exc:
            raise SkillError("policy_denied", str(exc)) from exc

    @staticmethod
    def _authorize_authentication(
        required: AuthenticationLevel,
        local_console_allowed: bool,
        context: AuthenticationContext | None,
        *,
        session_id: str,
        identity: str,
        action_digest: str | None,
    ) -> None:
        if required is AuthenticationLevel.NONE:
            return
        if context is None or not context.valid_for(
            session_id=session_id,
            identity=identity,
            now=time.time(),
            action_digest=action_digest,
        ):
            raise SkillError(
                "authentication_required",
                f"the action requires {required.value} authentication",
            )
        if required is AuthenticationLevel.FRESH:
            accepted = context.level is AuthenticationLevel.FRESH
        else:
            accepted = context.level in {
                AuthenticationLevel.ELEVATED,
                AuthenticationLevel.FRESH,
            } or (
                local_console_allowed
                and context.level is AuthenticationLevel.LOCAL_CONSOLE
            )
        if not accepted:
            raise SkillError(
                "authentication_required",
                f"the action requires {required.value} authentication",
            )


def allow_arguments(_arguments: Any) -> None:
    return None
