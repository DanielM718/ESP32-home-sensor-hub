"""Typed NAS Agent observations and the dormant fixed shutdown action."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping

from butters.actions.nas_agent import NasAgentHub
from butters.skills.model import (
    ActionClass,
    AuthenticationLevel,
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
    strict_arguments,
)


def _parse_none(values: Mapping[str, object]) -> SkillArguments:
    strict_arguments(values)
    return NoArguments()


def _job_idempotency_key(job_id: str | None) -> str:
    if (
        not isinstance(job_id, str)
        or not job_id
        or job_id.strip() != job_id
        or len(job_id) > 256
        or not job_id.isascii()
        or not job_id.isprintable()
    ):
        raise SkillError(
            "internal_error", "stable coordinator job identity is required"
        )
    value = bytearray(
        hashlib.sha256(b"butters-nas-action-job\0" + job_id.encode("ascii")).digest()[
            :16
        ]
    )
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(value)))


class NasAgentSkillImplementations:
    def __init__(self, hub: NasAgentHub) -> None:
        self.hub = hub

    @staticmethod
    def _result(kind: str, value: dict[str, object]) -> StructuredSkillResult:
        if not value.get("success"):
            code = value.get("error", "agent_action_failed")
            raise SkillError(
                code if isinstance(code, str) else "agent_action_failed",
                "NAS Agent operation failed",
            )
        return StructuredSkillResult(kind, value)

    def agent_status(self, _arguments: SkillArguments) -> StructuredSkillResult:
        return self._result("nas_agent_status", self.hub.agent_status())

    def system_status(self, _arguments: SkillArguments) -> StructuredSkillResult:
        return self._result("nas_system_status", self.hub.system_status())

    def jellyfin_status(self, _arguments: SkillArguments) -> StructuredSkillResult:
        return self._result("nas_jellyfin_status", self.hub.jellyfin_status())

    def shutdown(self, _arguments: SkillArguments) -> StructuredSkillResult:
        return self._result(
            "nas_shutdown",
            self.hub.shutdown(
                cancel=current_cancel_event(),
                idempotency_key=_job_idempotency_key(current_job_id()),
            ),
        )


def register_nas_agent_skills(
    registry: SkillRegistry,
    hub: NasAgentHub,
    *,
    shutdown_enabled: bool = False,
) -> None:
    implementation = NasAgentSkillImplementations(hub)
    configured = hub.settings.enabled and hub.configured
    empty_schema = {"type": "object", "properties": {}, "additionalProperties": False}

    for name, description, method, kind in (
        (
            "nas.agent.status",
            "Read bounded NAS Agent process metadata.",
            implementation.agent_status,
            "agent",
        ),
        (
            "nas.system.status",
            "Read bounded TrueNAS-local system state.",
            implementation.system_status,
            "system",
        ),
        (
            "nas.jellyfin.status",
            "Read bounded Jellyfin-local readiness.",
            implementation.jellyfin_status,
            "jellyfin",
        ),
    ):
        registry.register(
            SkillSpec(
                name=name,
                description=description,
                action_class=ActionClass.READ_ONLY,
                parse_arguments=_parse_none,
                authorize=allow_arguments,
                implementation=method,
                timeout_seconds=hub.settings.request_timeout_seconds + 2,
                category="nas_agent",
                input_schema=empty_schema,
                output_schema={"type": "object"},
                permission_summary=("read_only", "nas_agent", kind),
                audience=SkillAudience.ADMINISTRATOR,
                configured=configured,
                available=configured,
                unavailable_reason="NAS Agent ingress is disabled or unconfigured",
                source_reference="butters.skills.nas_agent",
            )
        )

    registry.register(
        SkillSpec(
            name="nas.system.shutdown",
            description="Ask the NAS Agent to invoke its fixed TrueNAS-local shutdown operation.",
            action_class=ActionClass.ACTION,
            parse_arguments=_parse_none,
            authorize=allow_arguments,
            implementation=implementation.shutdown,
            timeout_seconds=hub.settings.request_timeout_seconds + 2,
            category="nas_agent",
            input_schema=empty_schema,
            output_schema={"type": "object"},
            permission_summary=(
                "destructive",
                "nas_power",
                "fresh_passkey",
                "agent_fixed_operation",
            ),
            audience=SkillAudience.ADMINISTRATOR,
            explicit_intent_required=True,
            confirmation_required=True,
            side_effects="power off the one configured NAS",
            authentication=AuthenticationLevel.FRESH,
            configured=configured and shutdown_enabled,
            available=configured and shutdown_enabled,
            unavailable_reason="NAS Agent shutdown is disabled or unconfigured",
            source_reference="butters.skills.nas_agent",
        )
    )
