"""Deterministic authorization for one physical wake-word interaction."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

from butters.actions.coordinator import ActionCoordinator, ActionCoordinatorError
from butters.actions.store import ActionStateError, PendingPlan
from butters.assistant import AssistantResponse, DeterministicAssistant
from butters.routing.model import RoutedIntent
from butters.skills.model import AuthenticationContext, AuthenticationLevel
from butters.stt.normalization import normalize_transcript

AFFIRMATIVE = frozenset({"yes", "yeah", "confirm", "do it"})
NEGATIVE = frozenset({"no", "cancel", "don't", "dont", "stop"})


class LocalVoiceAuthorization:
    """Single-use LOCAL_CONSOLE context created only by the audio controller.

    This object is deliberately not used by browser or cloud request paths. A
    wake creates one short-lived local voice session. An allow-listed action
    and its exact deterministic confirmation both remain inside that session.
    """

    def __init__(
        self,
        assistant: DeterministicAssistant,
        coordinator: ActionCoordinator,
        *,
        context_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.assistant = assistant
        self.coordinator = coordinator
        self.context_seconds = context_seconds
        self.clock = clock
        self._lock = threading.RLock()
        self._voice_session_id = secrets.token_urlsafe(24)
        self._context_expires_at = 0.0
        self._pending: PendingPlan | None = None

    def note_physical_wake(self) -> None:
        """Create local authority from the fixed physical microphone path."""

        with self._lock:
            if self._pending is not None:
                # A new wake starts a different physical interaction and may
                # never revive or confirm the previous interaction's plan.
                self._cancel_pending(self._pending)
            self._voice_session_id = secrets.token_urlsafe(24)
            self._context_expires_at = self.clock() + self.context_seconds

    def cancel_pending_confirmation(self) -> None:
        """Cancel a pending action when the physical listening turn ends."""

        with self._lock:
            if self._pending is not None:
                self._cancel_pending(self._pending)
            self._context_expires_at = 0.0

    def handle_text(self, raw_text: str) -> AssistantResponse | None:
        started = time.perf_counter()
        normalized = normalize_transcript(raw_text.strip(), self.assistant.vocabulary)
        with self._lock:
            if self._pending is not None:
                return self._confirm(raw_text, normalized, started)
            route = self.assistant.preview_route(normalized)
            if not route.matched or route.skill is None:
                return None
            spec = self.assistant.skills.get(route.skill)
            if spec is None or spec.action_class.value != "action":
                return None
            plan_specs = tuple(
                self.assistant.skills.get(skill)
                for skill, _arguments in route.action_plan
            ) or (spec,)
            if self.clock() >= self._context_expires_at:
                return self._response(
                    raw_text,
                    normalized,
                    route,
                    "Say the configured wake phrase before requesting a local action.",
                    started,
                    "local_console_required",
                )
            if any(
                item is None
                or item.authentication is AuthenticationLevel.FRESH
                or not item.local_console_allowed
                for item in plan_specs
            ):
                self._context_expires_at = 0.0
                return self._response(
                    raw_text,
                    normalized,
                    route,
                    "That action requires passkey authentication. Open Butters on an authenticated device.",
                    started,
                    "fresh_authentication_required",
                )
            try:
                if route.action_plan:
                    self._pending = self.coordinator.freeze_plan(
                        steps=route.action_plan,
                        summary=" and ".join(
                            _action_summary(skill, arguments)
                            for skill, arguments in route.action_plan
                        ),
                        session_id=self._voice_session_id,
                        identity="local-console",
                        request_id=secrets.token_urlsafe(18),
                        source="physical_microphone",
                        pending_confirmation=True,
                    )
                else:
                    self._pending = self.coordinator.freeze(
                        skill=route.skill,
                        arguments=route.arguments,
                        summary=_action_summary(route.skill, route.arguments),
                        session_id=self._voice_session_id,
                        identity="local-console",
                        request_id=secrets.token_urlsafe(18),
                        source="physical_microphone",
                        pending_confirmation=True,
                    )
            except ActionCoordinatorError as exc:
                return self._response(
                    raw_text,
                    normalized,
                    route,
                    f"I can't prepare that action: {exc}",
                    started,
                    exc.code,
                )
            return self._response(
                raw_text,
                normalized,
                route,
                f"Are you asking me to {_spoken_summary(self._pending.summary)}?",
                started,
                "confirmation_required",
            )

    def _confirm(
        self,
        raw_text: str,
        normalized: str,
        started: float,
    ) -> AssistantResponse:
        plan = self._pending
        assert plan is not None
        route = RoutedIntent(
            "matched",
            normalized,
            plan.steps[0].skill,
            dict(plan.steps[0].arguments),
            confidence=1.0,
        )
        if self.clock() >= self._context_expires_at:
            self._cancel_pending(plan)
            return self._response(
                raw_text,
                normalized,
                route,
                "The confirmation expired. I did not perform the action.",
                started,
                "confirmation_expired",
            )
        # Every confirmation consumes the one physical voice-session context,
        # regardless of whether the recognized word is accepted.
        self._context_expires_at = 0.0
        if normalized in NEGATIVE:
            self._cancel_pending(plan)
            return self._response(
                raw_text,
                normalized,
                route,
                "Cancelled. I did not perform the action.",
                started,
                "cancelled",
            )
        if normalized not in AFFIRMATIVE:
            self._cancel_pending(plan)
            return self._response(
                raw_text,
                normalized,
                route,
                "I did not hear an exact confirmation, so I cancelled the action.",
                started,
                "confirmation_denied",
            )
        authentication = AuthenticationContext(
            AuthenticationLevel.LOCAL_CONSOLE,
            self._voice_session_id,
            "local-console",
            self.clock() + 5,
            "physical_wake_word_confirmation",
        )
        try:
            jobs = self.coordinator.execute(
                plan.plan_id,
                session_id=self._voice_session_id,
                identity="local-console",
                authentication=authentication,
            )
        except (ActionCoordinatorError, ActionStateError) as exc:
            code = getattr(exc, "code", "action_failed")
            self._pending = None
            return self._response(
                raw_text,
                normalized,
                route,
                f"I couldn't start the action: {exc}",
                started,
                code,
            )
        self._pending = None
        return self._response(
            raw_text,
            normalized,
            route,
            f"Confirmed. I started {_spoken_summary(plan.summary)}.",
            started,
            "action_started",
            jobs=jobs,
        )

    def _cancel_pending(self, plan: PendingPlan) -> None:
        try:
            self.coordinator.cancel_pending(
                plan.plan_id,
                session_id=self._voice_session_id,
                identity="local-console",
            )
        except ActionCoordinatorError:
            pass
        self._pending = None

    @staticmethod
    def _response(
        raw_text: str,
        normalized: str,
        route: RoutedIntent,
        text: str,
        started: float,
        status: str,
        *,
        jobs: tuple[dict[str, object], ...] = (),
    ) -> AssistantResponse:
        del jobs  # jobs are persisted and observed through the action store
        return AssistantResponse(
            raw_text,
            normalized,
            route,
            text,
            time.perf_counter() - started,
            routing_path="local_console_action",
            policy_status=status,
        )


def _action_summary(skill: str, arguments: dict[str, object]) -> str:
    if skill == "set_heater":
        return _environment_summary("heater", arguments)
    if skill == "set_dehumidifier":
        return _environment_summary("dehumidifier", arguments)
    if skill == "set_ventilation":
        return _environment_summary("ventilation", arguments)
    return skill.replace("_", " ")


def _environment_summary(device: str, arguments: dict[str, object]) -> str:
    state = str(arguments.get("state", ""))
    duration = arguments.get("duration_minutes")
    suffix = f" for {duration} minutes" if duration is not None else ""
    return f"turn {state} the {device}{suffix}"


def _spoken_summary(summary: str) -> str:
    return summary.rstrip(".?")
