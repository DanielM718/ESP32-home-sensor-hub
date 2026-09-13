"""Local-only Desktop Agent hardware-validation surface.

This process deliberately does not construct the browser service or its Admin
routes.  It owns one staging AgentHub and exposes four fixed validation calls
on a loopback listener; the separate TLS proxy can forward only the machine
WebSocket route.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import time
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path

import tomllib
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

from butters.actions.agent import AgentHub
from butters.actions.coordinator import ActionCoordinator, ActionCoordinatorError
from butters.actions.store import ActionStateError, ActionStateStore
from butters.assistant_config import ActionSettings, AgentIngressSettings
from butters.skills.desktop_agent import register_desktop_agent_skills
from butters.skills.model import (
    ActionClass,
    AuthenticationContext,
    AuthenticationLevel,
)
from butters.skills.policy import PolicyValidator
from butters.skills.registry import SkillRegistry

STAGING_CONFIG_ROOT = Path("/etc/butters-staging")
STAGING_STATE_ROOT = Path("/var/lib/butters-staging")
STAGING_IDENTITY = "desktop-agent-staging-validator"
STAGING_SESSION = "local-staging-hardware-validation"
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "expired"})


@dataclass(frozen=True, slots=True)
class StagingSettings:
    host: str
    port: int
    state_dir: Path
    agent_ingress: AgentIngressSettings
    actions: ActionSettings


def load_staging_settings(path: Path) -> StagingSettings:
    with path.open("rb") as source:
        data = tomllib.load(source)
    if set(data) != {"staging", "agent_ingress", "actions"}:
        raise ValueError("unexpected_staging_configuration")
    staging = data["staging"]
    agent = data["agent_ingress"]
    actions = data["actions"]
    if not all(type(value) is dict for value in (staging, agent, actions)):
        raise ValueError("invalid_staging_configuration")
    if set(staging) != {"host", "port", "state_dir"}:
        raise ValueError("invalid_staging_listener_configuration")
    if set(agent) - {
        "enabled",
        "config_path",
        "protocol_version",
        "hello_timeout_seconds",
        "socket_idle_seconds",
        "heartbeat_aging_seconds",
        "heartbeat_stale_seconds",
        "request_timeout_seconds",
    }:
        raise ValueError("invalid_staging_agent_configuration")
    if set(actions) != {"audit_capacity", "job_capacity"}:
        raise ValueError("invalid_staging_action_configuration")
    return StagingSettings(
        host=str(staging["host"]),
        port=int(staging["port"]),
        state_dir=Path(str(staging["state_dir"])),
        agent_ingress=AgentIngressSettings(
            enabled=agent.get("enabled") is True,
            config_path=Path(str(agent["config_path"])),
            protocol_version=int(agent.get("protocol_version", 1)),
            hello_timeout_seconds=float(agent.get("hello_timeout_seconds", 5.0)),
            socket_idle_seconds=float(agent.get("socket_idle_seconds", 60.0)),
            heartbeat_aging_seconds=float(
                agent.get("heartbeat_aging_seconds", 30.0)
            ),
            heartbeat_stale_seconds=float(
                agent.get("heartbeat_stale_seconds", 45.0)
            ),
            request_timeout_seconds=int(agent.get("request_timeout_seconds", 30)),
        ).validated(),
        actions=ActionSettings(
            audit_capacity=int(actions["audit_capacity"]),
            job_capacity=int(actions["job_capacity"]),
        ).validated(),
    )


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message}, status)


def validate_staging_settings(path: Path, settings: StagingSettings) -> None:
    """Fail closed before any state is opened or any listener is created."""

    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(STAGING_CONFIG_ROOT):
        raise ValueError("staging_config_root_required")
    if path.stat().st_mode & 0o022:
        raise ValueError("unsafe_staging_configuration")
    if settings.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("staging_loopback_required")
    if not 1024 <= settings.port <= 65535 or settings.port == 8090:
        raise ValueError("production_web_port_refused")
    state = settings.state_dir.resolve(strict=False)
    if not state.is_relative_to(STAGING_STATE_ROOT):
        raise ValueError("staging_state_root_required")
    agent_config = settings.agent_ingress.config_path.resolve(strict=False)
    if not agent_config.is_relative_to(STAGING_CONFIG_ROOT):
        raise ValueError("staging_agent_config_root_required")


class StagingValidationRuntime:
    """The bounded staging registry, policy, coordinator, and AgentHub."""

    def __init__(
        self,
        settings: StagingSettings,
        *,
        hub: AgentHub | None = None,
        state_dir: Path | None = None,
        policy: PolicyValidator | None = None,
    ) -> None:
        self.settings = settings
        self.hub = hub or AgentHub(settings.agent_ingress)
        self.policy = policy or PolicyValidator(
            allowed_actions=frozenset({ActionClass.READ_ONLY, ActionClass.ACTION})
        )
        self.registry = SkillRegistry(self.policy)
        register_desktop_agent_skills(self.registry, self.hub)
        database_root = Path(state_dir or settings.state_dir)
        self.store = ActionStateStore(
            database_root / "actions.sqlite3",
            settings.actions,
            pending_seconds=60,
        )
        self.store.recover_interrupted_jobs(local_console=False)
        self.coordinator = ActionCoordinator(self.registry, self.store)

    def observe(self, skill: str, arguments: dict[str, object]) -> dict[str, object]:
        execution = self.registry.execute(skill, arguments, administrator=True)
        if not execution.ok:
            assert execution.failure is not None
            return {
                "ok": False,
                "skill": skill,
                "error": execution.failure.code,
                "message": execution.failure.message,
            }
        value = asdict(execution.result) if is_dataclass(execution.result) else {}
        return {"ok": True, "skill": skill, "result": value}

    def launch(self, app: str) -> dict[str, object]:
        request_id = str(uuid.uuid4())
        try:
            plan = self.coordinator.freeze(
                skill="desktop.app.launch",
                arguments={"app": app},
                summary=f"Staging validation launch: {app}",
                session_id=STAGING_SESSION,
                identity=STAGING_IDENTITY,
                request_id=request_id,
                source="staging_local_validation_cli",
            )
            # This is not browser/passkey authentication.  It is a short-lived,
            # fixed-identity staging assertion which still traverses the normal
            # typed authentication and PolicyValidator checks.
            assertion = AuthenticationContext(
                AuthenticationLevel.ELEVATED,
                STAGING_SESSION,
                STAGING_IDENTITY,
                time.time() + 30,
                "staging_local_host_assertion",
            )
            jobs = self.coordinator.execute(
                plan.plan_id,
                session_id=STAGING_SESSION,
                identity=STAGING_IDENTITY,
                authentication=assertion,
            )
            job_id = str(jobs[0]["job_id"])
            deadline = (
                time.monotonic()
                + self.settings.agent_ingress.request_timeout_seconds
                + 8
            )
            while time.monotonic() < deadline:
                job = self.store.job(
                    job_id, session_id=STAGING_SESSION, identity=STAGING_IDENTITY
                )
                if job["state"] in TERMINAL_STATES:
                    return {
                        "ok": job["state"] == "completed",
                        "authorization": "staging_local_host_assertion",
                        "plan": plan.safe_dict(),
                        "job": job,
                    }
                time.sleep(0.05)
            return {
                "ok": False,
                "error": "validation_wait_timeout",
                "plan": plan.safe_dict(),
                "job": self.store.job(
                    job_id, session_id=STAGING_SESSION, identity=STAGING_IDENTITY
                ),
            }
        except (ActionCoordinatorError, ActionStateError) as exc:
            return {
                "ok": False,
                "error": exc.code,
                "message": str(exc),
            }


def create_app(runtime: StagingValidationRuntime) -> Starlette:
    async def local_call(request: Request, operation: str) -> Response:
        if not _is_loopback(request):
            return _error("local_only", "staging validation is loopback-only", 403)
        if (
            request.headers.get("content-length") not in {None, "0"}
            or request.headers.get("transfer-encoding") is not None
        ):
            return _error("body_refused", "request bodies are not accepted")
        app = request.path_params.get("app")
        if operation == "state":
            return JSONResponse({"ok": True, "state": runtime.hub.status()})
        if operation == "list":
            value = await asyncio.to_thread(runtime.observe, "desktop.app.list", {})
        elif operation == "status":
            value = await asyncio.to_thread(
                runtime.observe, "desktop.app.status", {"app": app}
            )
        else:
            value = await asyncio.to_thread(runtime.launch, str(app))
        return JSONResponse(value, status_code=200 if value.get("ok") else 409)

    async def state(request: Request) -> Response:
        return await local_call(request, "state")

    async def list_apps(request: Request) -> Response:
        return await local_call(request, "list")

    async def status(request: Request) -> Response:
        return await local_call(request, "status")

    async def launch(request: Request) -> Response:
        return await local_call(request, "launch")

    return Starlette(
        routes=[
            WebSocketRoute("/agent/v1/session", runtime.hub.socket),
            Route(
                "/validation/v1/state",
                state,
                methods=["GET"],
            ),
            Route(
                "/validation/v1/list-apps",
                list_apps,
                methods=["POST"],
            ),
            Route(
                "/validation/v1/status/{app:str}",
                status,
                methods=["POST"],
            ),
            Route(
                "/validation/v1/launch/{app:str}",
                launch,
                methods=["POST"],
            ),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated Desktop Agent staging")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "BUTTERS_STAGING_CONFIG", "/etc/butters-staging/assistant.toml"
            )
        ),
    )
    options = parser.parse_args()
    settings = load_staging_settings(options.config)
    validate_staging_settings(options.config, settings)
    runtime = StagingValidationRuntime(settings)
    uvicorn.run(
        create_app(runtime),
        host=settings.host,
        port=settings.port,
        workers=1,
        proxy_headers=False,
        access_log=False,
        log_level=os.environ.get("BUTTERS_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
