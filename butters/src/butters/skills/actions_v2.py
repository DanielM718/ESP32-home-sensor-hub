"""Semantic host, desktop, NAS, and environment capabilities."""

from __future__ import annotations

from typing import cast

from butters.actions.broker import BrokerOperation
from butters.assistant_config import ActionSettings, BrokerSettings, DesktopSettings
from butters.integrations.actions import (
    EnvironmentControlAdapter,
    FixedActionAdapter,
    HostStatusAdapter,
    NasAdapter,
)
from butters.skills.model import (
    ActionClass,
    AuthenticationLevel,
    DesktopArgs,
    EnvironmentActionArgs,
    NoArguments,
    SkillArguments,
    SkillResult,
    StructuredSkillResult,
)
from butters.skills.registry import (
    SkillRegistry,
    SkillSpec,
    current_cancel_event,
    current_job_id,
    required_string,
    strict_arguments,
)


class ActionSkillImplementations:
    def __init__(
        self,
        host: HostStatusAdapter,
        actions: FixedActionAdapter,
        nas: NasAdapter,
        environment: EnvironmentControlAdapter,
    ) -> None:
        self.host = host
        self.actions = actions
        self.nas = nas
        self.environment = environment

    @staticmethod
    def authorize_desktop(arguments: SkillArguments) -> None:
        if cast(DesktopArgs, arguments).machine != "desktop":
            raise ValueError("machine is not allow-listed")

    @staticmethod
    def authorize_none(_arguments: SkillArguments) -> None:
        return None

    def get_butters_host_status(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult("butters_host_status", self.host.host())

    def get_butters_service_status(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult("butters_service_status", self.host.service())

    def get_storage_status(self, _arguments: SkillArguments) -> SkillResult:
        host = self.host.host()
        return StructuredSkillResult(
            "storage_status",
            {
                "root_total_bytes": host["root_total_bytes"],
                "root_free_bytes": host["root_free_bytes"],
            },
        )

    def get_network_service_health(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult("network_service_health", self.host.dependencies())

    def get_action_broker_status(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult("action_broker_status", self.host.broker())

    def get_nas_status(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult("nas_status", self.nas.status())

    def get_environment_control_status(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult(
            "environment_control_status", self.environment.status()
        )

    def broker_action(self, operation: BrokerOperation) -> SkillResult:
        result = self.actions.execute(operation, cancel_event=current_cancel_event())
        return StructuredSkillResult("action_result", result)

    def wake_desktop(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_WAKE)

    def restore_local_desktop_session(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_RESTORE_LOCAL)

    def lock_desktop(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_LOCK)

    def sleep_desktop(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_SLEEP)

    def restart_desktop(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_RESTART)

    def shutdown_desktop(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.DESKTOP_SHUTDOWN)

    def restart_butters_service(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.HOST_RESTART_BUTTERS)

    def reboot_butters_host(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.HOST_REBOOT)

    def shutdown_butters_host(self, _arguments: SkillArguments) -> SkillResult:
        return self.broker_action(BrokerOperation.HOST_SHUTDOWN)

    def wake_nas(self, _arguments: SkillArguments) -> SkillResult:
        return StructuredSkillResult(
            "action_result", self.nas.wake(current_cancel_event())
        )

    def set_environment(self, device: str, arguments: SkillArguments) -> SkillResult:
        args = cast(EnvironmentActionArgs, arguments)
        result = self.environment.set(
            device,
            args.state,
            args.duration_minutes,
            cancel_event=current_cancel_event(),
            job_id=current_job_id() or "untracked",
        )
        return StructuredSkillResult("environment_action", result)


def register_action_skills(
    registry: SkillRegistry,
    *,
    desktop: DesktopSettings,
    broker: BrokerSettings,
    actions: ActionSettings,
    host: HostStatusAdapter,
    action_adapter: FixedActionAdapter,
    nas: NasAdapter,
    environment: EnvironmentControlAdapter,
) -> None:
    impl = ActionSkillImplementations(host, action_adapter, nas, environment)
    empty_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    desktop_schema = {
        "type": "object",
        "properties": {"machine": {"type": "string", "enum": ["desktop"]}},
        "required": ["machine"],
        "additionalProperties": False,
    }

    def read(name: str, description: str, method: object) -> None:
        registry.register(
            SkillSpec(
                name,
                description,
                ActionClass.READ_ONLY,
                _parse_none,
                impl.authorize_none,
                method,  # type: ignore[arg-type]
                5,
                version="2.1.0",
                category="read_only",
                input_schema=empty_schema,
                output_schema={"type": "object"},
                max_result_bytes=16384,
                source_reference="butters.skills.actions_v2",
            )
        )

    read(
        "get_butters_host_status",
        "Read bounded host load, memory, disk, uptime, and temperature.",
        impl.get_butters_host_status,
    )
    read(
        "get_butters_service_status",
        "Read fixed Butters service state and restart count.",
        impl.get_butters_service_status,
    )
    read(
        "get_storage_status",
        "Read bounded root-filesystem capacity.",
        impl.get_storage_status,
    )
    read(
        "get_network_service_health",
        "Read fixed dependency service health.",
        impl.get_network_service_health,
    )
    read(
        "get_action_broker_status",
        "Read action broker provisioning/readiness state.",
        impl.get_action_broker_status,
    )
    read(
        "get_nas_status",
        "Read configured NAS capability status without arbitrary targeting.",
        impl.get_nas_status,
    )
    read(
        "get_environment_control_status",
        "Read configured heater, dehumidifier, ventilation, and override status.",
        impl.get_environment_control_status,
    )

    def action(
        name: str,
        description: str,
        method: object,
        *,
        authentication: AuthenticationLevel,
        available: bool,
        local_console: bool = False,
        schema: dict[str, object] = desktop_schema,
        parser: object = _parse_desktop,
        timeout: float = 30,
    ) -> None:
        registry.register(
            SkillSpec(
                name,
                description,
                ActionClass.ACTION,
                parser,  # type: ignore[arg-type]
                impl.authorize_desktop
                if parser is _parse_desktop
                else impl.authorize_none,
                method,  # type: ignore[arg-type]
                timeout,
                version="2.1.0",
                category="action",
                input_schema=schema,
                output_schema={"type": "object"},
                explicit_intent_required=True,
                confirmation_required=True,
                side_effects=description,
                authentication=authentication,
                local_console_allowed=local_console,
                configured=available,
                available=available and broker.enabled,
                unavailable_reason=None
                if available and broker.enabled
                else "capability is disabled or the action broker is unprovisioned",
                source_reference="butters.skills.actions_v2",
            )
        )

    action(
        "wake_desktop",
        "Wake the one configured desktop.",
        impl.wake_desktop,
        authentication=AuthenticationLevel.ELEVATED,
        available=desktop.wake_enabled,
        local_console=True,
    )
    action(
        "restore_local_desktop_session",
        "Restore the configured desktop local display mode.",
        impl.restore_local_desktop_session,
        authentication=AuthenticationLevel.ELEVATED,
        available=desktop.restore_enabled,
        local_console=True,
    )
    action(
        "lock_desktop",
        "Lock the configured desktop session.",
        impl.lock_desktop,
        authentication=AuthenticationLevel.ELEVATED,
        available=desktop.lock_enabled,
    )
    action(
        "sleep_desktop",
        "Put the configured desktop to sleep.",
        impl.sleep_desktop,
        authentication=AuthenticationLevel.FRESH,
        available=desktop.sleep_enabled,
    )
    action(
        "restart_desktop",
        "Restart the configured desktop.",
        impl.restart_desktop,
        authentication=AuthenticationLevel.FRESH,
        available=desktop.restart_enabled,
    )
    action(
        "shutdown_desktop",
        "Shut down the configured desktop.",
        impl.shutdown_desktop,
        authentication=AuthenticationLevel.FRESH,
        available=desktop.shutdown_enabled,
    )
    action(
        "restart_butters_service",
        "Schedule restart of the fixed Butters web service.",
        impl.restart_butters_service,
        authentication=AuthenticationLevel.FRESH,
        available=actions.host_restart_butters_enabled,
        schema=empty_schema,
        parser=_parse_none,
    )
    action(
        "reboot_butters_host",
        "Reboot the Butters host.",
        impl.reboot_butters_host,
        authentication=AuthenticationLevel.FRESH,
        available=actions.host_reboot_enabled,
        schema=empty_schema,
        parser=_parse_none,
    )
    action(
        "shutdown_butters_host",
        "Shut down the Butters host.",
        impl.shutdown_butters_host,
        authentication=AuthenticationLevel.FRESH,
        available=actions.host_shutdown_enabled,
        schema=empty_schema,
        parser=_parse_none,
    )
    action(
        "wake_nas",
        "Wake the one configured NAS.",
        impl.wake_nas,
        authentication=AuthenticationLevel.ELEVATED,
        available=actions.nas.enabled and actions.nas.configured,
        local_console=actions.nas.local_console_allowed,
        schema=empty_schema,
        parser=_parse_none,
    )

    for device, config in (
        ("heater", actions.heater),
        ("dehumidifier", actions.dehumidifier),
        ("ventilation", actions.ventilation),
    ):
        schema = {
            "type": "object",
            "properties": {
                "state": {"type": "string", "enum": ["on", "off"]},
                "duration_minutes": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": config.maximum_duration_minutes,
                },
            },
            "required": ["state", "duration_minutes"],
            "additionalProperties": False,
        }
        action(
            f"set_{device}",
            f"Set the configured {device} state with an optional bounded duration.",
            lambda arguments, selected=device: impl.set_environment(
                selected, arguments
            ),
            authentication=AuthenticationLevel.ELEVATED,
            available=config.enabled
            and config.configured
            and not (config.require_fresh_sensor and not config.safety_entity),
            local_console=config.local_console_allowed,
            schema=schema,
            parser=lambda values, maximum=config.maximum_duration_minutes: (
                _parse_environment(values, maximum)
            ),
            timeout=config.maximum_duration_minutes * 60 + 30,
        )


def _parse_none(values: dict[str, object]) -> NoArguments:
    strict_arguments(values)
    return NoArguments()


def _parse_desktop(values: dict[str, object]) -> DesktopArgs:
    strict_arguments(values, required=frozenset({"machine"}))
    machine = required_string(values, "machine")
    if len(machine) > 16:
        raise ValueError("machine is too long")
    return DesktopArgs(machine)


def _parse_environment(
    values: dict[str, object], maximum: int
) -> EnvironmentActionArgs:
    strict_arguments(
        values,
        required=frozenset({"state", "duration_minutes"}),
    )
    state = required_string(values, "state")
    if len(state) > 3:
        raise ValueError("state is too long")
    if state not in {"on", "off"}:
        raise ValueError("state must be on or off")
    raw_duration = values.get("duration_minutes")
    if raw_duration is None:
        duration = None
    elif (
        isinstance(raw_duration, bool)
        or not isinstance(raw_duration, int)
        or not 1 <= raw_duration <= maximum
    ):
        raise ValueError("duration_minutes exceeds the configured limit")
    else:
        duration = raw_duration
    if state == "off" and duration is not None:
        raise ValueError("off does not accept a duration")
    return EnvironmentActionArgs(state, duration)
