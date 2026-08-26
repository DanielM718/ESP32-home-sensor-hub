"""Freeze, authenticate, execute, observe, and audit bounded action plans."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, is_dataclass

from butters.actions.store import (
    ActionStateError,
    ActionStateStore,
    FrozenStep,
    PendingPlan,
)
from butters.skills.model import (
    ActionAuthorization,
    AuthenticationContext,
    AuthenticationLevel,
)
from butters.skills.registry import SkillRegistry

LOGGER = logging.getLogger(__name__)


class ActionCoordinatorError(PermissionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActionCoordinator:
    def __init__(self, registry: SkillRegistry, store: ActionStateStore) -> None:
        self.registry = registry
        self.store = store
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def freeze(
        self,
        *,
        skill: str,
        arguments: dict[str, object],
        summary: str,
        session_id: str,
        identity: str,
        request_id: str,
        source: str,
        pending_confirmation: bool = False,
    ) -> PendingPlan:
        return self.freeze_plan(
            steps=((skill, arguments),),
            summary=summary,
            session_id=session_id,
            identity=identity,
            request_id=request_id,
            source=source,
            pending_confirmation=pending_confirmation,
        )

    def freeze_plan(
        self,
        *,
        steps: tuple[tuple[str, dict[str, object]], ...],
        summary: str,
        session_id: str,
        identity: str,
        request_id: str,
        source: str,
        pending_confirmation: bool = False,
    ) -> PendingPlan:
        frozen: list[FrozenStep] = []
        requirements: list[AuthenticationLevel] = []
        for skill, arguments in steps:
            canonical, failure = self.registry.validate_action_intent(skill, arguments)
            if failure is not None or canonical is None:
                raise ActionCoordinatorError(
                    failure.code if failure else "policy_denied",
                    failure.message if failure else "action cannot be frozen",
                )
            spec = self.registry.get(skill)
            assert spec is not None
            frozen.append(FrozenStep(skill, canonical))
            requirements.append(spec.authentication)
        authentication = (
            AuthenticationLevel.FRESH
            if AuthenticationLevel.FRESH in requirements
            else AuthenticationLevel.ELEVATED
        )
        try:
            return self.store.freeze(
                steps=tuple(frozen),
                summary=summary,
                session_id=session_id,
                identity=identity,
                request_id=request_id,
                source=source,
                authentication=authentication,
                state="pending_confirmation"
                if pending_confirmation
                else "pending_auth",
            )
        except ActionStateError as exc:
            raise ActionCoordinatorError(exc.code, str(exc)) from exc

    def execute(
        self,
        plan_id: str,
        *,
        session_id: str,
        identity: str,
        authentication: AuthenticationContext,
    ) -> tuple[dict[str, object], ...]:
        try:
            plan = self.store.require(
                plan_id,
                session_id=session_id,
                identity=identity,
                allowed_states=frozenset({"pending_auth", "pending_confirmation"}),
            )
            if plan.authentication is AuthenticationLevel.FRESH and (
                authentication.level is not AuthenticationLevel.FRESH
                or authentication.action_digest != plan.digest
            ):
                raise ActionCoordinatorError(
                    "fresh_authentication_required",
                    "fresh authentication is not bound to this action plan",
                )
            self.store.claim(plan)
        except ActionStateError as exc:
            raise ActionCoordinatorError(exc.code, str(exc)) from exc
        first = plan.steps[0]
        try:
            job_id = self.store.create_job(plan, first)
        except ActionStateError as exc:
            self.store.set_plan_state(plan.plan_id, "failed")
            raise ActionCoordinatorError(exc.code, str(exc)) from exc
        cancel_event = threading.Event()
        with self._lock:
            self._cancel_events[job_id] = cancel_event
        worker = threading.Thread(
            target=self._run_plan,
            args=(plan, job_id, authentication, cancel_event),
            name="butters-action-" + job_id[:8],
            daemon=True,
        )
        worker.start()
        return (self.store.job(job_id, session_id=session_id, identity=identity),)

    def cancel_job(
        self, job_id: str, *, session_id: str, identity: str
    ) -> dict[str, object]:
        job = self.store.job(job_id, session_id=session_id, identity=identity)
        if job["state"] not in {"queued", "running", "waiting"}:
            raise ActionCoordinatorError(
                "cancellation_unavailable", "the action can no longer be cancelled"
            )
        with self._lock:
            event = self._cancel_events.get(job_id)
        if event is None:
            raise ActionCoordinatorError(
                "cancellation_unavailable",
                "the action is not cancellable in this process",
            )
        event.set()
        return self.store.job(job_id, session_id=session_id, identity=identity)

    def cancel_pending(self, plan_id: str, *, session_id: str, identity: str) -> None:
        try:
            self.store.cancel_plan(plan_id, session_id=session_id, identity=identity)
        except ActionStateError as exc:
            raise ActionCoordinatorError(exc.code, str(exc)) from exc

    def _run_plan(
        self,
        plan: PendingPlan,
        job_id: str,
        authentication: AuthenticationContext,
        cancel_event: threading.Event,
    ) -> None:
        started = time.perf_counter()
        self.store.update_job(job_id, state="running", stage="policy")
        authorization = ActionAuthorization(
            frozenset(item.skill for item in plan.steps),
            "confirmed_user_request"
            if plan.state == "pending_confirmation"
            else "direct_user_request",
            True,
        )
        final_results: list[dict[str, object]] = []
        try:
            for index, step in enumerate(plan.steps):
                if cancel_event.is_set():
                    raise ActionCoordinatorError("cancelled", "action was cancelled")
                self.store.update_job(
                    job_id,
                    state="running",
                    stage=f"execute_{index + 1}_of_{len(plan.steps)}",
                    progress=index / len(plan.steps),
                )
                execution = self.registry.execute(
                    step.skill,
                    step.arguments,
                    administrator=True,
                    action_authorization=authorization,
                    authentication_context=authentication,
                    session_id=plan.session_id,
                    identity=plan.identity,
                    action_digest=plan.digest,
                    cancel_event=cancel_event,
                    job_id=job_id,
                )
                if not execution.ok:
                    assert execution.failure is not None
                    raise ActionCoordinatorError(
                        execution.failure.code, execution.failure.message
                    )
                value = (
                    asdict(execution.result)
                    if is_dataclass(execution.result)
                    else {"result": str(execution.result)}
                )
                final_results.append({"skill": step.skill, "result": value})
                self.store.audit(
                    identity=plan.identity,
                    session_id=plan.session_id,
                    skill=step.skill,
                    authentication=authentication.level,
                    method=authentication.method,
                    arguments=step.arguments,
                    outcome="completed",
                    job_id=job_id,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                )
            self.store.update_job(
                job_id,
                state="completed",
                stage="completed",
                progress=1.0,
                result={"steps": final_results},
            )
            self.store.set_plan_state(plan.plan_id, "completed")
        except ActionCoordinatorError as exc:
            state = "cancelled" if exc.code == "cancelled" else "failed"
            self.store.update_job(
                job_id,
                state=state,
                stage=state,
                failure_code=exc.code,
                failure_reason=str(exc),
            )
            self.store.set_plan_state(plan.plan_id, state)
            for step in plan.steps:
                self.store.audit(
                    identity=plan.identity,
                    session_id=plan.session_id,
                    skill=step.skill,
                    authentication=authentication.level,
                    method=authentication.method,
                    arguments=step.arguments,
                    outcome=state,
                    job_id=job_id,
                    elapsed_ms=round((time.perf_counter() - started) * 1000),
                    reason_code=exc.code,
                )
        except Exception:
            LOGGER.exception("action job failed safely")
            try:
                self.store.update_job(
                    job_id,
                    state="failed",
                    stage="failed",
                    failure_code="internal_error",
                    failure_reason="the action job failed safely",
                )
                self.store.set_plan_state(plan.plan_id, "failed")
            except Exception:
                LOGGER.exception("action job failure state could not be persisted")
        finally:
            with self._lock:
                self._cancel_events.pop(job_id, None)
