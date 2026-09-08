"""Interactive actions registered in the existing deterministic skill system."""

from dataclasses import dataclass

from butters_agent.protocol import MUTATIONS, SCHEMAS, parameters
from butters.skills.model import (ActionClass, AuthenticationLevel, SkillAudience,
                                  SkillError, StructuredSkillResult)
from butters.skills.policy import allow_arguments
from butters.skills.registry import SkillSpec, current_cancel_event


@dataclass(frozen=True)
class EmptyArgs:
    pass


@dataclass(frozen=True)
class AppArgs:
    app: str


@dataclass(frozen=True)
class VmArgs:
    vm: str


def register_agent_skills(registry, hub, streaming=None):
    for action in SCHEMAS:
        def parse(values, action=action):
            values = parameters(action, dict(values))
            return AppArgs(**values) if "app" in values else VmArgs(**values) if "vm" in values else EmptyArgs()

        def invoke(arguments, action=action):
            from dataclasses import asdict
            if action == "desktop.agent.status":
                result = {"success": True, **hub.status()}
            else:
                result = hub.invoke(action, asdict(arguments), cancel=current_cancel_event())
            if not result.get("success"):
                raise SkillError(result.get("error", "agent_action_failed"), "Desktop Agent action failed")
            return StructuredSkillResult("desktop_agent", result)

        mutation = action in MUTATIONS
        registry.register(SkillSpec(
            name=action, description="Registered interactive desktop operation: " + action,
            action_class=ActionClass.ACTION if mutation else ActionClass.READ_ONLY,
            parse_arguments=parse, authorize=allow_arguments, implementation=invoke,
            timeout_seconds=36, audience=SkillAudience.ADMINISTRATOR,
            category="desktop_agent", explicit_intent_required=mutation,
            authentication=AuthenticationLevel.FRESH if action == "desktop.vm.stop" else
                AuthenticationLevel.ELEVATED if mutation else AuthenticationLevel.NONE,
            confirmation_required=action == "desktop.vm.stop",
            side_effects="launch registered application or VM" if mutation else "none",
            input_schema={"type": "object", "additionalProperties": False,
                "properties": {key: {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,63}$"}
                               for key in SCHEMAS[action]}, "required": sorted(SCHEMAS[action])},
            permission_summary=("routine" if mutation else "read_only",),
        ))
    if streaming is not None:
        for action in ("desktop.streaming.status", "desktop.streaming.prepare"):
            mutation = action.endswith("prepare")
            def parse_streaming(values):
                if values:
                    raise ValueError("Streaming accepts no parameters")
                return EmptyArgs()
            def invoke_streaming(arguments, mutation=mutation):
                result = streaming.prepare(current_cancel_event()) if mutation else streaming.status()
                return StructuredSkillResult("desktop_streaming", result)
            registry.register(SkillSpec(name=action, description="Prepare or inspect the fixed streaming desktop",
                action_class=ActionClass.ACTION if mutation else ActionClass.READ_ONLY,
                parse_arguments=parse_streaming, authorize=allow_arguments, implementation=invoke_streaming,
                timeout_seconds=290 if mutation else 40, audience=SkillAudience.ADMINISTRATOR,
                authentication=AuthenticationLevel.ELEVATED if mutation else AuthenticationLevel.NONE,
                explicit_intent_required=mutation, category="desktop_agent",
                side_effects="WOL, Parsec service ensure, registered Parsec launch" if mutation else "none"))
