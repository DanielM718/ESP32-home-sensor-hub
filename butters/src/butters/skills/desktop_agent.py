"""Deterministic Desktop Agent application skills.

The agent owns executable mappings. These skills accept symbolic identifiers
only and remain absent from both conversational catalogs.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import cast

from butters.actions.agent import AgentHub
from butters.skills.model import (
    ActionClass,
    AuthenticationLevel,
    DesktopAppArgs,
    NoArguments,
    SkillArguments,
    SkillAudience,
    SkillError,
    StructuredSkillResult,
)
from butters.skills.policy import allow_arguments
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    current_cancel_event,
    current_job_id,
    required_string,
    strict_arguments,
)


def _parse_list(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values)
    return NoArguments()


def _parse_app(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values, required=frozenset({"app"}))
    app = required_string(values, "app")
    from butters_agent.protocol import NAME

    if not NAME.fullmatch(app):
        raise SkillError("invalid_arguments", "app must be a symbolic identifier")
    return DesktopAppArgs(app)


class DesktopAgentSkillImplementations:
    def __init__(self, hub: AgentHub) -> None:
        self.hub = hub

    @staticmethod
    def _result(kind: str, value: dict[str, object]) -> StructuredSkillResult:
        if not value.get("success"):
            code = value.get("error", "agent_action_failed")
            if not isinstance(code, str):
                code = "agent_action_failed"
            raise SkillError(code, "Desktop Agent application operation failed")
        return StructuredSkillResult(kind, value)

    def list_apps(self, _arguments: SkillArguments) -> StructuredSkillResult:
        return self._result("desktop_app_catalog", self.hub.list_apps())

    def app_status(self, arguments: SkillArguments) -> StructuredSkillResult:
        app = cast(DesktopAppArgs, arguments).app
        return self._result("desktop_app_status", self.hub.app_status(app))

    def launch_app(self, arguments: SkillArguments) -> StructuredSkillResult:
        app = cast(DesktopAppArgs, arguments).app
        job_id = current_job_id()
        idempotency_key = (
            str(uuid.uuid5(uuid.NAMESPACE_URL, "butters-action-job:" + job_id))
            if job_id
            else None
        )
        return self._result(
            "desktop_app_launch",
            self.hub.launch_app(
                app,
                cancel=current_cancel_event(),
                idempotency_key=idempotency_key,
            ),
        )


def register_desktop_agent_skills(registry: SkillRegistry, hub: AgentHub) -> None:
    """Register Slice 3 only; no route or planner registration occurs here."""

    implementation = DesktopAgentSkillImplementations(hub)
    configured = hub.settings.enabled and hub.configured
    empty_schema = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    app_schema = {
        "type": "object",
        "properties": {
            "app": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{0,63}$",
                "maxLength": 64,
            }
        },
        "required": ["app"],
        "additionalProperties": False,
    }

    def register_read(
        name: str, method: object, schema: dict[str, object], parser: object
    ) -> None:
        registry.register(
            SkillSpec(
                name=name,
                description="Read the Desktop Agent's allowlisted application catalog."
                if name.endswith("list")
                else "Read truthful state for one agent-allowlisted application.",
                action_class=ActionClass.READ_ONLY,
                parse_arguments=parser,  # type: ignore[arg-type]
                authorize=allow_arguments,
                implementation=method,  # type: ignore[arg-type]
                timeout_seconds=hub.settings.request_timeout_seconds + 2,
                version="1.0.0",
                category="desktop_agent",
                input_schema=schema,
                output_schema={"type": "object"},
                permission_summary=("read_only", "agent_allowlist"),
                audience=SkillAudience.ADMINISTRATOR,
                configured=configured,
                available=configured,
                unavailable_reason="Desktop Agent ingress is disabled or unconfigured",
                source_reference="butters.skills.desktop_agent",
            )
        )

    register_read(
        "desktop.app.list", implementation.list_apps, empty_schema, _parse_list
    )
    register_read(
        "desktop.app.status", implementation.app_status, app_schema, _parse_app
    )
    registry.register(
        SkillSpec(
            name="desktop.app.launch",
            description="Launch one application from the Desktop Agent's local allowlist.",
            action_class=ActionClass.ACTION,
            parse_arguments=_parse_app,
            authorize=allow_arguments,
            implementation=implementation.launch_app,
            timeout_seconds=hub.settings.request_timeout_seconds + 2,
            version="1.0.0",
            category="desktop_agent",
            input_schema=app_schema,
            output_schema={"type": "object"},
            permission_summary=("routine", "agent_allowlist", "interactive_session"),
            audience=SkillAudience.ADMINISTRATOR,
            explicit_intent_required=True,
            confirmation_required=False,
            side_effects="launch one agent-allowlisted application",
            authentication=AuthenticationLevel.ELEVATED,
            configured=configured,
            available=configured,
            unavailable_reason="Desktop Agent ingress is disabled or unconfigured",
            source_reference="butters.skills.desktop_agent",
        )
    )
