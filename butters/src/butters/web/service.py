"""One orchestration path shared by browser text and browser voice."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, is_dataclass, replace
from enum import Enum
from pathlib import Path

from butters.actions.coordinator import ActionCoordinator, ActionCoordinatorError
from butters.actions.store import ActionStateStore
from butters.assistant import (
    AssistantResponse,
    DeterministicAssistant,
    create_assistant,
)
from butters.assistant_config import AssistantSettings
from butters.auth.manager import PasskeyManager
from butters.auth.store import AuthStateStore
from butters.cloud.general import GeneralCloudReasoner, OpenAIGeneralReasoner
from butters.cloud.model import (
    CloudReasonerError,
    CloudTokenUsage,
    EscalationLevel,
    ReasoningConfiguration,
)
from butters.cloud.openai_responses import OpenAIResponsesReasoner
from butters.cloud.orchestrator import CloudDiagnosticEscalator
from butters.cloud.usage import UsageLedger
from butters.diagnostics.engine import DiagnosticEngine
from butters.diagnostics.model import DiagnosticRequest, RequestDepth
from butters.diagnostics.sanitizer import sanitize_value
from butters.remediation.skill_builder import CodexSkillBuilder
from butters.routing.conversation import route_conversation_turn
from butters.routing.model import RoutedIntent
from butters.skills.model import (
    ActionAuthorization,
    ActionClass,
    AuthenticationLevel,
)
from butters.stt.normalization import DomainVocabulary, normalize_transcript
from butters.web.sessions import BrowserSession, SessionManager
from butters.web.speech import (
    LocalTTSProvider,
    OpenAISTTProvider,
    OpenAITTSProvider,
    SpeechProviderError,
    SpeechResult,
    TranscriptionResult,
    VoicePreset,
    VoicePresetStore,
    validate_preset,
)
from butters.web.trace import ExecutionTrace, TraceBuffer, TraceStage


class RouteOverride(str, Enum):
    AUTO = "auto"
    DETERMINISTIC_LOCAL = "deterministic_local"
    LOCAL_DIAGNOSTIC = "local_diagnostic"
    CLOUD_AUTO = "cloud_auto"
    FORCE_CLOUD_MODEL = "force_cloud_model"
    CLOUD_DISABLED = "cloud_disabled"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    selected_route: str
    reason_codes: tuple[str, ...]
    features: dict[str, object]
    admin_override: str | None = None
    model_avoided: bool = True


@dataclass(frozen=True, slots=True)
class ServiceResponse:
    trace_id: str
    request_id: str
    response_text: str
    normalized_text: str
    route: str
    reason_codes: tuple[str, ...]
    skill: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    usage: dict[str, object] | None = None
    stopping_reason: str | None = None
    authentication_required: str | None = None
    pending_action: dict[str, object] | None = None
    jobs: tuple[dict[str, object], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class _ForcedDiagnosticPolicy:
    """One-request diagnostic model override; it never changes global policy."""

    def __init__(self, model: str, effort: str) -> None:
        self.configuration = ReasoningConfiguration(
            EscalationLevel.ANALYSIS,
            model,
            effort,
        )

    def initial(self, _request: object, _local: object) -> ReasoningConfiguration:
        return self.configuration

    def next(
        self,
        _current: ReasoningConfiguration,
        _request: object,
        _conclusion: object,
    ) -> None:
        return None


class BetaAssistantService:
    def __init__(
        self,
        settings: AssistantSettings,
        vocabulary: DomainVocabulary,
        *,
        assistant: DeterministicAssistant | None = None,
        general_reasoner: GeneralCloudReasoner | None = None,
        ledger: UsageLedger | None = None,
        sessions: SessionManager | None = None,
        traces: TraceBuffer | None = None,
        state_dir: Path | None = None,
        local_tts: LocalTTSProvider | None = None,
    ) -> None:
        self.settings = settings
        self.vocabulary = vocabulary
        self.state_dir = Path(state_dir or settings.web.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.action_state = ActionStateStore(
            self.state_dir / "actions.sqlite3",
            settings.actions,
            pending_seconds=settings.authentication.pending_action_seconds,
        )
        self.action_state.recover_interrupted_jobs(local_console=False)
        self.assistant = assistant or create_assistant(
            settings, vocabulary, action_state=self.action_state
        )
        if self.assistant.environment_adapter is not None:
            self.assistant.environment_adapter.recover_overrides()
        self.auth_state = AuthStateStore(
            self.state_dir / "security.sqlite3", settings.authentication
        )
        self.passkeys = PasskeyManager(self.auth_state, settings.authentication)
        self.actions = ActionCoordinator(self.assistant.skills, self.action_state)
        self.ledger = ledger or UsageLedger(
            settings.cloud,
            self.state_dir / "usage.sqlite3",
        )
        self.traces = traces or TraceBuffer(
            settings.web.trace_capacity,
            ttl_seconds=settings.web.trace_ttl_seconds,
        )
        self.sessions = sessions or SessionManager(
            max_active=settings.web.max_active_sessions,
            ttl_seconds=settings.web.session_ttl_seconds,
            max_messages=settings.web.max_messages_per_session,
            max_context_chars=settings.web.max_context_chars,
            max_per_peer=settings.web.max_sessions_per_peer,
            admin_reserve=settings.web.admin_session_reserve,
            # Conversation text lives in traces too, so an expiring session takes
            # its traces with it rather than leaving them for the trace TTL.
            on_expire=self.traces.drop_sessions,
        )
        self.general_reasoner = general_reasoner or OpenAIGeneralReasoner(
            settings.cloud
        )
        self.voice_presets = VoicePresetStore(self.state_dir / "state.sqlite3")
        self.skill_builder = CodexSkillBuilder(
            settings.remediation,
            self.state_dir / "skill-jobs.sqlite3",
        )
        self.local_tts = local_tts or LocalTTSProvider(self._local_tts_engine)
        self.cloud_stt = OpenAISTTProvider(settings)
        self.cloud_tts = OpenAITTSProvider(settings)
        # A single daemon owns paid-provider accounting. Serializing the
        # permit/call/record sequence prevents concurrent requests from all
        # passing a stale daily or monthly budget check.
        self._paid_operation_gate = threading.Lock()
        self._wire_diagnostic_cloud()

    def handle_text(
        self,
        session: BrowserSession,
        raw_text: str,
        *,
        source: str = "text",
        override: RouteOverride = RouteOverride.AUTO,
        forced_model: str | None = None,
        reasoning_effort: str = "medium",
        max_output_tokens: int | None = None,
        administrator: bool = False,
        trace: ExecutionTrace | None = None,
    ) -> ServiceResponse:
        # One browser conversation has one ordered turn stream.  This makes the
        # read/update/clear of pending clarification state atomic even when a
        # client accidentally submits overlapping HTTP and voice turns.
        with session.turn_lock:
            return self._handle_text_locked(
                session,
                raw_text,
                source=source,
                override=override,
                forced_model=forced_model,
                reasoning_effort=reasoning_effort,
                max_output_tokens=max_output_tokens,
                administrator=administrator,
                trace=trace,
            )

    def _handle_text_locked(
        self,
        session: BrowserSession,
        raw_text: str,
        *,
        source: str,
        override: RouteOverride,
        forced_model: str | None,
        reasoning_effort: str,
        max_output_tokens: int | None,
        administrator: bool,
        trace: ExecutionTrace | None,
    ) -> ServiceResponse:
        started = time.perf_counter()
        text = " ".join(raw_text.replace("\x00", "").split())
        if not text or len(text) > 4000:
            raise ValueError("text must contain 1 to 4000 characters")
        if override is not RouteOverride.AUTO and not administrator:
            raise PermissionError("route overrides require administrator authorization")
        if forced_model is not None and override is not RouteOverride.FORCE_CLOUD_MODEL:
            raise ValueError("forced_model requires force_cloud_model override")
        if reasoning_effort not in {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            raise ValueError("reasoning effort is not allow-listed")
        output_limit = (
            self.settings.cloud.max_output_tokens
            if max_output_tokens is None
            else max_output_tokens
        )
        if not 64 <= output_limit <= self.settings.cloud.max_output_tokens:
            raise ValueError("max_output_tokens exceeds the configured ceiling")

        current_trace = trace or self.traces.start(session.session_id, source)
        current_trace.emit(
            TraceStage.REQUEST,
            "accepted",
            fields={
                "raw_text": text,
                "source": source,
                "admin_override": override.value if administrator else None,
            },
        )
        self.sessions.add_message(session, "user", text, current_trace.trace_id)
        normalized = normalize_transcript(text, self.vocabulary)
        current_trace.emit(
            TraceStage.NORMALIZATION,
            "complete",
            fields={
                "normalized_text": normalized,
                "changed": normalized != text.casefold(),
            },
        )

        clarification_disposition = "standalone"
        if administrator or override is not RouteOverride.AUTO:
            route = self.assistant.router.route(normalized)
        else:
            conversational = route_conversation_turn(
                self.assistant.router,
                normalized,
                session.pending_clarification,
                now=time.monotonic(),
                ttl_seconds=self.settings.web.clarification_timeout_seconds,
            )
            route = conversational.route
            session.pending_clarification = conversational.pending
            clarification_disposition = conversational.disposition
        deterministic_reason = (
            "clarification_resolved"
            if clarification_disposition == "resolved" and route.matched
            else "deterministic_skill_match"
        )
        if (
            route.matched
            and route.skill is not None
            and not administrator
            and self.assistant.skills.requires_administrator(route.skill)
        ):
            # Administrator-sensitive observations are refused before any skill,
            # diagnostic, or cloud stage runs: the ordinary surface must neither
            # answer them nor escalate them to a paid model to try.
            current_trace.emit(
                TraceStage.POLICY,
                "denied",
                reason_code="administrator_required",
                fields={"skill": route.skill, "audience": "administrator"},
            )
            denied_route = RoutedIntent(
                "unsupported",
                normalized,
                message="I can't answer that request from the normal chat.",
            )
            result = self._unsupported_response(
                text, normalized, denied_route, "administrator_required"
            )
            decision = RouteDecision(
                "unsupported",
                ("administrator_required",),
                _denied_features(normalized),
                None,
                True,
            )
            response = self._from_assistant_result(result, current_trace, decision)
            return self._finish(session, current_trace, response, started)
        diagnostic_request = self._diagnostic_request(normalized, override)
        if (
            diagnostic_request is not None
            and route.matched
            and override is RouteOverride.AUTO
            and not self._explicit_diagnostic_language(normalized)
        ):
            diagnostic_request = None
        features = self._complexity_features(
            normalized, route, diagnostic_request, override
        )
        current_trace.emit(
            TraceStage.ROUTING,
            route.status,
            reason_code=deterministic_reason
            if route.matched
            else "missing_required_argument"
            if route.incomplete
            else "unsupported_intent",
            fields={
                "candidate": route.skill,
                "arguments": route.arguments,
                "missing_arguments": list(route.missing_arguments),
                "confidence": route.confidence if route.matched else None,
                "diagnostic_domain": diagnostic_request.domain.value
                if diagnostic_request
                else None,
                "clarification_disposition": clarification_disposition,
                "aggregate": route.aggregate,
                "ambiguity_candidates": list(route.ambiguity_candidates),
            },
        )
        current_trace.emit(TraceStage.COMPLEXITY, "classified", fields=features)

        matched_spec = (
            self.assistant.skills.get(route.skill)
            if route.matched and route.skill is not None
            else None
        )
        if (
            matched_spec is not None
            and matched_spec.action_class is ActionClass.ACTION
            and override is RouteOverride.AUTO
        ):
            response = self._prepare_browser_action(
                session,
                text,
                normalized,
                route,
                current_trace,
                source=source,
            )
            return self._finish(session, current_trace, response, started)

        if override is RouteOverride.LOCAL_DIAGNOSTIC:
            if diagnostic_request is None:
                result = self._unsupported_response(
                    text, normalized, route, "diagnostic_playbook_not_matched"
                )
                decision = RouteDecision(
                    "unsupported",
                    ("diagnostic_playbook_not_matched",),
                    features,
                    override.value,
                )
            else:
                result = self._execute_diagnostic(
                    diagnostic_request, current_trace, force_cloud=False
                )
                decision = RouteDecision(
                    "diagnostic_local",
                    ("diagnostic_playbook_match",),
                    features,
                    override.value,
                )
        elif (
            diagnostic_request is not None
            and override is not RouteOverride.DETERMINISTIC_LOCAL
        ):
            force_cloud = override in {
                RouteOverride.CLOUD_AUTO,
                RouteOverride.FORCE_CLOUD_MODEL,
            }
            result = self._execute_diagnostic(
                diagnostic_request,
                current_trace,
                force_cloud=force_cloud,
                forced_model=forced_model
                if override is RouteOverride.FORCE_CLOUD_MODEL
                else None,
                forced_effort=reasoning_effort,
            )
            if override is RouteOverride.FORCE_CLOUD_MODEL:
                reasons = ("admin_forced_cloud",)
            elif override is RouteOverride.CLOUD_AUTO:
                reasons = ("admin_cloud_auto",)
            elif override is RouteOverride.CLOUD_DISABLED:
                reasons = ("diagnostic_playbook_match", "cloud_disabled")
            else:
                reasons = ("diagnostic_playbook_match",)
            decision = RouteDecision(
                result.routing_path,
                reasons,
                features,
                override.value if administrator else None,
                not result.diagnostic.cloud_used if result.diagnostic else True,
            )
        elif (
            route.matched
            and (
                features["open_ended_causal_request"]
                or route.skill == "analyze_print_environment"
            )
            and override is RouteOverride.AUTO
        ):
            if self.general_reasoner.available:
                response = self._general_cloud(
                    session,
                    text,
                    current_trace,
                    model=self.settings.cloud.terra_model,
                    effort="high",
                    max_output_tokens=output_limit,
                    route=route,
                    reason_code="open_ended_reasoning_required",
                    administrator=administrator,
                )
                return self._finish(session, current_trace, response, started)
            result = self._execute_skill(
                text, normalized, route, current_trace, administrator
            )
            result = replace(
                result,
                response_text=(
                    result.response_text
                    + " Cloud reasoning is currently unavailable, so I won't provide a cloud interpretation."
                ),
            )
            decision = RouteDecision(
                "deterministic",
                (
                    deterministic_reason,
                    "open_ended_reasoning_required",
                    "cloud_disabled",
                ),
                features,
                None,
                True,
            )
        elif route.matched and override in {
            RouteOverride.AUTO,
            RouteOverride.DETERMINISTIC_LOCAL,
            RouteOverride.CLOUD_DISABLED,
        }:
            result = self._execute_skill(
                text, normalized, route, current_trace, administrator
            )
            decision = RouteDecision(
                "deterministic",
                (deterministic_reason,),
                features,
                override.value if administrator else None,
                True,
            )
        elif override in {RouteOverride.CLOUD_AUTO, RouteOverride.FORCE_CLOUD_MODEL}:
            model = forced_model or self.settings.cloud.terra_model
            response = self._general_cloud(
                session,
                text,
                current_trace,
                model=model,
                effort=reasoning_effort,
                max_output_tokens=output_limit,
                route=route,
                reason_code="admin_forced_cloud",
                administrator=administrator,
            )
            return self._finish(session, current_trace, response, started)
        elif route.incomplete:
            result = self._unsupported_response(
                text, normalized, route, "missing_required_argument"
            )
            decision = RouteDecision(
                "clarification",
                ("missing_required_argument",),
                features,
                override.value if administrator else None,
            )
        elif override in {
            RouteOverride.DETERMINISTIC_LOCAL,
            RouteOverride.CLOUD_DISABLED,
        }:
            code = (
                "cloud_disabled"
                if override is RouteOverride.CLOUD_DISABLED
                else "unsupported_intent"
            )
            result = self._unsupported_response(text, normalized, route, code)
            decision = RouteDecision(
                "unsupported", (code,), features, override.value, True
            )
        elif self.general_reasoner.available:
            response = self._general_cloud(
                session,
                text,
                current_trace,
                model=self.settings.cloud.terra_model,
                effort="high",
                max_output_tokens=output_limit,
                route=route,
                reason_code="open_ended_reasoning_required",
                administrator=administrator,
            )
            return self._finish(session, current_trace, response, started)
        else:
            result = self._unsupported_response(
                text, normalized, route, "cloud_disabled"
            )
            decision = RouteDecision(
                "unsupported",
                ("open_ended_reasoning_required", "cloud_disabled"),
                features,
                None,
                True,
            )

        current_trace.emit(
            TraceStage.ROUTING,
            "selected",
            reason_code=decision.reason_codes[0],
            fields=asdict(decision),
        )
        response = self._from_assistant_result(result, current_trace, decision)
        return self._finish(session, current_trace, response, started)

    def _prepare_browser_action(
        self,
        session: BrowserSession,
        raw_text: str,
        normalized: str,
        route: RoutedIntent,
        trace: ExecutionTrace,
        *,
        source: str,
    ) -> ServiceResponse:
        assert route.skill is not None
        spec = self.assistant.skills.get(route.skill)
        assert spec is not None
        if not session.administrator:
            trace.emit(
                TraceStage.POLICY,
                "denied",
                reason_code="administrator_required",
                fields={"skill": route.skill},
            )
            self.action_state.audit(
                identity=session.peer_key,
                session_id=session.session_id,
                skill=route.skill,
                authentication=AuthenticationLevel.NONE,
                method="tailnet_identity",
                arguments=route.arguments,
                outcome="denied",
                job_id=None,
                reason_code="administrator_required",
            )
            return ServiceResponse(
                trace.trace_id,
                trace.request_id,
                "This action requires an authorized administrator identity and passkey authentication.",
                normalized,
                "action_denied",
                ("administrator_required",),
                skill=route.skill,
                stopping_reason="administrator_required",
            )
        try:
            if route.action_plan:
                plan = self.actions.freeze_plan(
                    steps=route.action_plan,
                    summary=_plan_summary(route.action_plan),
                    session_id=session.session_id,
                    identity=session.peer_key,
                    request_id=trace.request_id,
                    source=source,
                )
            else:
                plan = self.actions.freeze(
                    skill=route.skill,
                    arguments=route.arguments,
                    summary=_action_summary(route.skill, route.arguments),
                    session_id=session.session_id,
                    identity=session.peer_key,
                    request_id=trace.request_id,
                    source=source,
                )
        except ActionCoordinatorError as exc:
            trace.emit(
                TraceStage.POLICY,
                "denied",
                reason_code=exc.code,
                fields={"skill": route.skill, "arguments": route.arguments},
            )
            self.action_state.audit(
                identity=session.peer_key,
                session_id=session.session_id,
                skill=route.skill,
                authentication=AuthenticationLevel.NONE,
                method="tailnet_identity",
                arguments=route.arguments,
                outcome="denied",
                job_id=None,
                reason_code=exc.code,
            )
            return ServiceResponse(
                trace.trace_id,
                trace.request_id,
                "That capability is not currently available.",
                normalized,
                "action_denied",
                (exc.code,),
                skill=route.skill,
                stopping_reason=exc.code,
            )
        elevation = self.auth_state.elevation(session.session_id, session.peer_key)
        if (
            plan.authentication is AuthenticationLevel.ELEVATED
            and elevation is not None
        ):
            jobs = self.actions.execute(
                plan.plan_id,
                session_id=session.session_id,
                identity=session.peer_key,
                authentication=elevation,
            )
            trace.emit(
                TraceStage.POLICY,
                "authenticated",
                fields={
                    "skill": route.skill,
                    "authentication": elevation.level.value,
                    "job_ids": [item["job_id"] for item in jobs],
                },
            )
            return ServiceResponse(
                trace.trace_id,
                trace.request_id,
                "The exact action has been queued.",
                normalized,
                "action_job",
                ("authenticated_action",),
                skill=route.skill,
                jobs=jobs,
            )
        trace.emit(
            TraceStage.POLICY,
            "authentication_required",
            reason_code=plan.authentication.value,
            fields={
                "skill": route.skill,
                "pending_action_id": plan.plan_id,
                "authentication": plan.authentication.value,
                "action_digest": plan.digest[:12],
            },
        )
        self.action_state.audit(
            identity=session.peer_key,
            session_id=session.session_id,
            skill="action_plan" if len(plan.steps) > 1 else route.skill,
            authentication=plan.authentication,
            method="pending_webauthn",
            arguments={
                "steps": [
                    {"skill": step.skill, "arguments": step.arguments}
                    for step in plan.steps
                ]
            },
            outcome="pending_auth",
            job_id=None,
        )
        return ServiceResponse(
            trace.trace_id,
            trace.request_id,
            "Authentication is required. Use your passkey to authorize this exact action.",
            normalized,
            "pending_auth",
            ("authentication_required",),
            skill=route.skill,
            authentication_required=plan.authentication.value,
            pending_action=plan.safe_dict(),
            stopping_reason="authentication_required",
        )

    def authentication_status(self, session: BrowserSession) -> dict[str, object]:
        return self.passkeys.status(session.session_id, session.peer_key)

    def lock_elevation(self, session: BrowserSession) -> dict[str, object]:
        self.auth_state.lock(session.session_id)
        return self.authentication_status(session)

    def begin_authentication(
        self,
        session: BrowserSession,
        *,
        purpose: str,
        pending_action_id: str | None = None,
        subject: str | None = None,
    ) -> dict[str, object]:
        self._require_action_admin(session)
        digest = None
        required = AuthenticationLevel.ELEVATED
        if purpose == "pending_action":
            if pending_action_id is None:
                raise ActionCoordinatorError(
                    "pending_action_required", "pending action ID is required"
                )
            try:
                plan = self.action_state.require(
                    pending_action_id,
                    session_id=session.session_id,
                    identity=session.peer_key,
                )
            except Exception as exc:  # normalized at the API boundary
                code = getattr(exc, "code", "pending_action_denied")
                raise ActionCoordinatorError(code, str(exc)) from exc
            digest = plan.digest
            required = plan.authentication
        return self.passkeys.begin_authentication(
            session_id=session.session_id,
            identity=session.peer_key,
            purpose=purpose,
            action_digest=digest,
            pending_action_id=pending_action_id,
            subject=subject,
            required_level=required,
        )

    def finish_authentication(
        self,
        session: BrowserSession,
        *,
        ceremony_id: str,
        credential: dict[str, object],
    ) -> dict[str, object]:
        self._require_action_admin(session)
        outcome = self.passkeys.finish_authentication(
            ceremony_id=ceremony_id,
            session_id=session.session_id,
            identity=session.peer_key,
            credential=credential,
        )
        jobs: tuple[dict[str, object], ...] = ()
        if outcome.pending_action_id is not None and outcome.context is not None:
            jobs = self.actions.execute(
                outcome.pending_action_id,
                session_id=session.session_id,
                identity=session.peer_key,
                authentication=outcome.context,
            )
        return {
            "verified": True,
            "purpose": outcome.purpose,
            "status": self.authentication_status(session),
            "jobs": list(jobs),
            "fresh_grant": outcome.fresh_grant,
        }

    def begin_passkey_registration(
        self,
        session: BrowserSession,
        *,
        label: str,
        bootstrap_token: str | None,
        fresh_grant: str | None,
    ) -> dict[str, object]:
        self._require_action_admin(session)
        return self.passkeys.begin_registration(
            session_id=session.session_id,
            identity=session.peer_key,
            label=label,
            bootstrap_token=bootstrap_token,
            fresh_grant=fresh_grant,
        )

    def finish_passkey_registration(
        self,
        session: BrowserSession,
        *,
        ceremony_id: str,
        credential: dict[str, object],
    ) -> dict[str, object]:
        self._require_action_admin(session)
        result = self.passkeys.finish_registration(
            ceremony_id=ceremony_id,
            session_id=session.session_id,
            identity=session.peer_key,
            credential=credential,
        )
        method = str(result.pop("authorization_method"))
        self.action_state.audit(
            identity=session.peer_key,
            session_id=session.session_id,
            skill="passkey.register",
            authentication=(
                AuthenticationLevel.FRESH
                if method == "fresh_webauthn"
                else AuthenticationLevel.NONE
            ),
            method=method,
            arguments={"label": result.get("label")},
            outcome="completed",
            job_id=None,
        )
        return result

    def passkey_credentials(
        self, session: BrowserSession
    ) -> tuple[dict[str, object], ...]:
        self._require_action_admin(session)
        return tuple(
            item.safe_dict() for item in self.auth_state.credentials(session.peer_key)
        )

    def label_passkey(
        self, session: BrowserSession, record_id: str, label: str
    ) -> None:
        self._require_action_admin(session)
        self.auth_state.label_credential(record_id, session.peer_key, label)

    def revoke_passkey(
        self, session: BrowserSession, record_id: str, fresh_grant: str
    ) -> None:
        self._require_action_admin(session)
        subject = self.auth_state.consume_fresh_grant(
            fresh_grant,
            session_id=session.session_id,
            identity=session.peer_key,
            purpose="revoke_passkey",
        )
        if subject != record_id:
            raise ActionCoordinatorError(
                "fresh_binding_denied", "fresh authorization targets another credential"
            )
        self.auth_state.revoke_credential(record_id, session.peer_key)
        self.auth_state.lock(session.session_id)
        self.action_state.audit(
            identity=session.peer_key,
            session_id=session.session_id,
            skill="passkey.revoke",
            authentication=AuthenticationLevel.FRESH,
            method="fresh_webauthn",
            arguments={"credential_record": record_id},
            outcome="completed",
            job_id=None,
        )

    def action_job(self, session: BrowserSession, job_id: str) -> dict[str, object]:
        return self.action_state.job(
            job_id, session_id=session.session_id, identity=session.peer_key
        )

    def cancel_action_job(
        self, session: BrowserSession, job_id: str
    ) -> dict[str, object]:
        return self.actions.cancel_job(
            job_id, session_id=session.session_id, identity=session.peer_key
        )

    def cancel_pending_action(self, session: BrowserSession, plan_id: str) -> None:
        self.actions.cancel_pending(
            plan_id, session_id=session.session_id, identity=session.peer_key
        )

    def capability_status(
        self, *, administrator: bool = False
    ) -> tuple[dict[str, object], ...]:
        return self.assistant.skills.metadata(administrator=administrator)

    @staticmethod
    def _require_action_admin(session: BrowserSession) -> None:
        if not session.administrator:
            raise ActionCoordinatorError(
                "administrator_required",
                "authorized administrator identity is required",
            )

    def synthesize_trace_response(
        self,
        session: BrowserSession,
        trace_id: str,
        *,
        preset: VoicePreset | None = None,
    ) -> SpeechResult:
        trace = self.traces.get(trace_id)
        if trace is None or trace.session_id != session.session_id:
            raise SpeechProviderError("trace_denied", "response trace is unavailable")
        message = next(
            (
                item.text
                for item in reversed(session.messages)
                if item.role == "assistant" and item.trace_id == trace_id
            ),
            None,
        )
        if message is None:
            raise SpeechProviderError(
                "trace_denied", "response trace has no assistant message"
            )
        selected = preset or self.voice_presets.default(self.settings)
        started = time.perf_counter()
        result = self.synthesize_preview(
            message,
            selected,
            request_id=trace.request_id,
            session_id=session.session_id,
        )
        trace.emit(
            TraceStage.TTS,
            "complete",
            fields={
                "provider": result.provider,
                "model": result.model,
                "voice": result.voice,
                "generation_latency_ms": round(result.generation_seconds * 1000, 3),
                "audio_seconds": round(result.audio_seconds, 3),
                "estimated_cost_usd": result.estimated_cost_usd,
                "request_latency_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        )
        return result

    def synthesize_preview(
        self,
        text: str,
        preset: VoicePreset,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
    ) -> SpeechResult:
        # Previews and saved presets share one validation boundary, so an
        # out-of-range or non-finite speed is rejected before any engine loads.
        validate_preset(preset, settings=self.settings)
        if preset.provider == "local":
            return self.local_tts.synthesize(text, preset)
        if preset.provider != "openai":
            raise SpeechProviderError("provider_denied", "TTS provider is not allowed")
        price = self.settings.providers.cloud_tts_price_per_million_characters_usd
        estimate = float("inf") if price is None else len(text) * price / 1_000_000
        with self._paid_operation_gate:
            if not self.ledger.permits(estimate):
                raise SpeechProviderError(
                    "budget_denied", "paid TTS was blocked by the configured budget"
                )
            try:
                result = self.cloud_tts.synthesize(text, preset)
            except SpeechProviderError as exc:
                self.ledger.record_external(
                    provider="openai",
                    operation_category="tts",
                    model=preset.model,
                    estimated_cost_usd=estimate,
                    wall_seconds=0.0,
                    success=False,
                    request_id=request_id,
                    session_id=session_id,
                    error_code=exc.code,
                )
                raise
            if result.estimated_cost_usd is None:
                raise SpeechProviderError(
                    "pricing_unknown", "paid TTS pricing is unavailable"
                )
            self.ledger.record_external(
                provider=result.provider,
                operation_category="tts",
                model=result.model,
                estimated_cost_usd=result.estimated_cost_usd,
                wall_seconds=result.generation_seconds,
                success=True,
                request_id=request_id,
                session_id=session_id,
            )
        return result

    def transcribe_cloud_preview(
        self,
        audio_wav: bytes,
        *,
        duration_seconds: float,
        session_id: str | None = None,
    ) -> TranscriptionResult:
        price = self.settings.providers.cloud_stt_price_per_minute_usd
        estimate = float("inf") if price is None else duration_seconds / 60 * price
        with self._paid_operation_gate:
            if not self.ledger.permits(estimate):
                raise SpeechProviderError(
                    "budget_denied", "paid STT was blocked by the configured budget"
                )
            try:
                result = self.cloud_stt.transcribe_wav(
                    audio_wav,
                    duration_seconds=duration_seconds,
                )
            except SpeechProviderError as exc:
                self.ledger.record_external(
                    provider="openai",
                    operation_category="stt",
                    model=self.settings.providers.cloud_stt_model,
                    estimated_cost_usd=estimate,
                    wall_seconds=0.0,
                    success=False,
                    session_id=session_id,
                    error_code=exc.code,
                )
                raise
            if result.estimated_cost_usd is None:
                raise SpeechProviderError(
                    "pricing_unknown", "paid STT pricing is unavailable"
                )
            self.ledger.record_external(
                provider=result.provider,
                operation_category="stt",
                model=result.model,
                estimated_cost_usd=result.estimated_cost_usd,
                wall_seconds=result.elapsed_seconds,
                success=True,
                session_id=session_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        return result

    def usage_report(self, session_id: str | None = None) -> dict[str, object]:
        """Bounded administrator usage view; runs on a worker, never the loop."""

        return {
            "summary": self.ledger.summary(),
            "current_session": (
                self.ledger.summary(session_id=session_id) if session_id else None
            ),
            "recent": self.ledger.recent(100),
            "recent_requests": self.ledger.recent_requests(100),
        }

    def repository_status(self) -> dict[str, object]:
        adapter = getattr(self.assistant, "project_adapter", None)
        configured = self.settings.remediation.project_inspection_root is not None
        return {
            "configured": configured,
            "available": bool(adapter is not None and adapter.available),
            "write_authority": False,
            "reason": None
            if configured
            else "no repository is configured for this deployment",
        }

    def clear_conversation(self, session: BrowserSession) -> None:
        """Clear a conversation and the detailed traces that quote it."""

        with session.turn_lock:
            self.sessions.clear(session)
            self.traces.drop_sessions((session.session_id,))

    def credential_status(self) -> dict[str, object]:
        return {
            "openai": {
                "configured": bool(os.getenv("OPENAI_API_KEY")),
                "provider": "openai",
                "last_verification": None,
            },
            "paid_text_enabled": self.settings.cloud.enabled
            and self.settings.cloud.allow_paid_calls,
            "paid_stt_enabled": self.settings.providers.allow_paid_stt,
            "paid_tts_enabled": self.settings.providers.allow_paid_tts,
        }

    def _execute_skill(
        self,
        raw: str,
        normalized: str,
        route: RoutedIntent,
        trace: ExecutionTrace,
        administrator: bool = False,
    ) -> AssistantResponse:
        assert route.skill is not None
        spec = self.assistant.skills.get(route.skill)
        action_authorization = None
        if (
            spec is not None
            and spec.action_class is ActionClass.ACTION
            and route.skill == "start_remote_desktop_session"
            and _direct_desktop_action(normalized)
        ):
            action_authorization = ActionAuthorization(
                frozenset({route.skill}), "direct_user_request", True
            )
        trace.emit(
            TraceStage.SKILL,
            "requested",
            fields={"skill": route.skill, "arguments": route.arguments},
        )
        validation = self.assistant.skills.validate_proposal(
            route.skill,
            route.arguments,
            administrator=administrator,
            action_authorization=action_authorization,
        )
        trace.emit(
            TraceStage.POLICY,
            "allowed" if validation is None else "denied",
            reason_code=None if validation is None else validation.code,
            fields={
                "skill": route.skill,
                "action_class": spec.action_class.value if spec else None,
                "action_authorized": action_authorization is not None,
                "authorization_source": action_authorization.source
                if action_authorization
                else None,
            },
        )
        if validation is not None and validation.code == "administrator_required":
            # Do not confirm that an administrator-sensitive observation exists.
            return self._unsupported_response(
                raw,
                normalized,
                RoutedIntent(
                    "unsupported",
                    normalized,
                    message="I can't answer that request from the normal chat.",
                ),
                "administrator_required",
            )
        execution = self.assistant.skills.execute(
            route.skill,
            route.arguments,
            administrator=administrator,
            action_authorization=action_authorization,
        )
        trace.emit(
            TraceStage.TOOL,
            "complete" if execution.ok else "failed",
            reason_code=execution.failure.code if execution.failure else None,
            fields={
                "skill": route.skill,
                "arguments": route.arguments,
                "latency_ms": round(execution.elapsed_seconds * 1000, 3),
                "result_summary": _bounded_result(execution.result),
            },
        )
        return AssistantResponse(
            raw,
            normalized,
            route,
            self.assistant.formatter.format_execution(execution),
            execution.elapsed_seconds,
            execution,
            routing_path="deterministic",
            policy_status="allowed" if execution.ok else "denied",
        )

    def _execute_diagnostic(
        self,
        request: DiagnosticRequest,
        trace: ExecutionTrace,
        *,
        force_cloud: bool,
        forced_model: str | None = None,
        forced_effort: str = "high",
    ) -> AssistantResponse:
        engine = self.assistant.diagnostic_engine
        if engine is None:
            route = RoutedIntent(
                "unsupported", request.text, message="Local diagnostics are disabled."
            )
            return self._unsupported_response(
                request.text, request.text, route, "diagnostics_disabled"
            )
        if force_cloud:
            request = replace(
                request,
                depth=RequestDepth.DETAILED,
                local_only=False,
                allow_cloud=True,
            )
        trace.emit(
            TraceStage.TOOL,
            "diagnostic_plan_started",
            reason_code="diagnostic_playbook_match",
            fields={
                "domain": request.domain.value,
                "target": request.target,
                "force_cloud": force_cloud,
                "forced_model": forced_model,
                "forced_effort": forced_effort if forced_model else None,
            },
        )
        started = time.perf_counter()
        selected_engine = engine
        if forced_model is not None:
            selected_engine = DiagnosticEngine(
                engine.planner,
                engine.tools,
                rules=engine.rules,
                cloud=CloudDiagnosticEscalator(
                    OpenAIResponsesReasoner(self.settings.cloud),
                    engine.tools,
                    self.settings.cloud,
                    policy=_ForcedDiagnosticPolicy(forced_model, forced_effort),
                    ledger=self.ledger,
                ),
                session_ttl_seconds=engine.session_ttl_seconds,
                max_evidence_bytes=engine.max_evidence_bytes,
            )
        with self._paid_operation_gate:
            with self.ledger.request_context(
                request_id=trace.request_id,
                session_id=trace.session_id,
                route_category="diagnostic_cloud",
            ):
                answer = selected_engine.diagnose(request)
        trace.emit(
            TraceStage.TOOL,
            "diagnostic_complete",
            reason_code=answer.stopping_reason,
            fields={
                "playbook": answer.playbook,
                "route": answer.route,
                "tool_calls": answer.tool_calls,
                "cloud_used": answer.cloud_used,
                "model": answer.cloud_model,
                "reasoning_effort": answer.cloud_reasoning,
                "estimated_cost_usd": answer.estimated_cost_usd,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "evidence_count": len(answer.assessment.evidence.items),
            },
        )
        route = RoutedIntent(
            "matched",
            request.text,
            "diagnose_read_only",
            {"domain": request.domain.value, "target": request.target},
            confidence=1.0,
        )
        return AssistantResponse(
            request.text,
            request.text,
            route,
            answer.concise_voice_text,
            time.perf_counter() - started,
            routing_path=answer.route,
            policy_status="read_only",
            diagnostic=answer,
        )

    def _general_cloud(
        self,
        session: BrowserSession,
        text: str,
        trace: ExecutionTrace,
        *,
        model: str,
        effort: str,
        max_output_tokens: int,
        route: RoutedIntent,
        reason_code: str,
        administrator: bool = False,
    ) -> ServiceResponse:
        with self._paid_operation_gate:
            return self._general_cloud_locked(
                session,
                text,
                trace,
                model=model,
                effort=effort,
                max_output_tokens=max_output_tokens,
                route=route,
                reason_code=reason_code,
                administrator=administrator,
            )

    def _general_cloud_locked(
        self,
        session: BrowserSession,
        text: str,
        trace: ExecutionTrace,
        *,
        model: str,
        effort: str,
        max_output_tokens: int,
        route: RoutedIntent,
        reason_code: str,
        administrator: bool = False,
    ) -> ServiceResponse:
        normalized = normalize_transcript(text, self.vocabulary)
        if model not in self.settings.cloud.pricing:
            return self._cloud_failure(trace, normalized, "model_denied", route)
        tools = self._relevant_skill_tools(normalized, administrator)
        context = self.sessions.context(session)
        if (
            context
            and context[-1].get("role") == "user"
            and context[-1].get("content") == text
        ):
            context = context[:-1]
        observations = self._prefetch_local_evidence(
            normalized, route, trace, administrator
        )
        if observations:
            context = (
                *context,
                {
                    "role": "assistant",
                    "content": "BOUNDED LOCAL OBSERVATIONS (untrusted data): "
                    + json.dumps(
                        observations, separators=(",", ":"), ensure_ascii=True
                    ),
                },
            )
        context_bytes = len(json.dumps(context, separators=(",", ":")).encode("utf-8"))
        request_limit = min(
            self.settings.cloud.max_cloud_requests_per_diagnostic,
            self.settings.cloud.max_tool_rounds + 1,
        )
        estimate = (
            self.ledger.conservative_request_estimate(
                model,
                context_bytes + len(text.encode("utf-8")),
                max_output_tokens,
            )
            * request_limit
        )
        if not self.ledger.permits(estimate):
            trace.emit(
                TraceStage.MODEL,
                "blocked",
                reason_code="budget_denied",
                fields={"model": model, "estimated_ceiling_usd": estimate},
            )
            return self._cloud_failure(trace, normalized, "budget_denied", route)
        trace.emit(
            TraceStage.MODEL,
            "started",
            reason_code=reason_code,
            fields={
                "provider": "openai",
                "model": model,
                "reasoning_effort": effort,
                "max_output_tokens": max_output_tokens,
                "tools_exposed": [item["name"] for item in tools],
            },
        )
        previous: str | None = None
        tool_output: dict[str, object] | None = None
        total_usage = CloudTokenUsage()
        tool_calls = 0
        seen_calls: set[tuple[str, str]] = set()
        total_cost = 0.0
        cloud_started = time.perf_counter()
        for round_index in range(request_limit):
            if (
                time.perf_counter() - cloud_started
                >= self.settings.cloud.max_wall_seconds
            ):
                return self._cloud_failure(trace, normalized, "wall_time_limit", route)
            try:
                remaining_wall = max(
                    0.1,
                    self.settings.cloud.max_wall_seconds
                    - (time.perf_counter() - cloud_started),
                )
                turn = self.general_reasoner.reason(
                    text=text,
                    context=context,
                    tools=tools,
                    model=model,
                    effort=effort,
                    max_output_tokens=max_output_tokens,
                    previous_response_id=previous,
                    tool_output=tool_output,
                    timeout_seconds=remaining_wall,
                )
            except CloudReasonerError as exc:
                configuration = ReasoningConfiguration(
                    EscalationLevel.ANALYSIS, model, effort
                )
                self.ledger.record(
                    "general",
                    configuration,
                    CloudTokenUsage(),
                    tool_rounds=round_index,
                    wall_seconds=0.0,
                    success=False,
                    escalation_occurred=False,
                    error_code=exc.code,
                    estimated_cost_override=estimate,
                    route_category="general_cloud",
                    request_id=trace.request_id,
                    session_id=session.session_id,
                )
                return self._cloud_failure(trace, normalized, exc.code, route)
            configuration = ReasoningConfiguration(
                EscalationLevel.ANALYSIS, model, effort
            )
            record = self.ledger.record(
                "general",
                configuration,
                turn.usage,
                tool_rounds=round_index,
                tool_calls=tool_calls,
                wall_seconds=turn.elapsed_seconds,
                success=True,
                escalation_occurred=False,
                route_category="general_cloud",
                request_id=trace.request_id,
                session_id=session.session_id,
            )
            total_cost += record.estimated_cost_usd
            total_usage = CloudTokenUsage(
                total_usage.input_tokens + turn.usage.input_tokens,
                total_usage.cached_tokens + turn.usage.cached_tokens,
                total_usage.cache_write_tokens + turn.usage.cache_write_tokens,
                total_usage.output_tokens + turn.usage.output_tokens,
                total_usage.reasoning_tokens + turn.usage.reasoning_tokens,
            )
            trace.emit(
                TraceStage.MODEL,
                "turn_complete",
                reason_code=turn.stopping_reason,
                fields={
                    "provider_request_id": turn.response_id,
                    "model": model,
                    "reasoning_effort": effort,
                    "latency_ms": round(turn.elapsed_seconds * 1000, 3),
                    "input_tokens": turn.usage.input_tokens,
                    "cached_tokens": turn.usage.cached_tokens,
                    "output_tokens": turn.usage.output_tokens,
                    "reasoning_tokens": turn.usage.reasoning_tokens,
                    "estimated_cost_usd": record.estimated_cost_usd,
                },
            )
            if turn.response_text is not None:
                return ServiceResponse(
                    trace.trace_id,
                    trace.request_id,
                    turn.response_text,
                    normalized,
                    "general_cloud",
                    (reason_code,),
                    model=model,
                    reasoning_effort=effort,
                    usage={
                        **asdict(total_usage),
                        "estimated_cost_usd": round(total_cost, 8),
                        "tool_calls": tool_calls,
                    },
                    stopping_reason=turn.stopping_reason,
                )
            if turn.tool_request is None:
                return self._cloud_failure(
                    trace, normalized, "cloud_no_conclusion", route
                )
            tool_calls += 1
            if tool_calls > self.settings.cloud.max_total_tool_calls:
                return self._cloud_failure(trace, normalized, "tool_call_limit", route)
            request = turn.tool_request
            canonical_call = (
                request.name,
                json.dumps(request.arguments, sort_keys=True, separators=(",", ":")),
            )
            if canonical_call in seen_calls:
                return self._cloud_failure(
                    trace, normalized, "repeated_tool_call", route
                )
            seen_calls.add(canonical_call)
            failure = self.assistant.skills.validate_proposal(
                request.name, request.arguments, administrator=administrator
            )
            trace.emit(
                TraceStage.POLICY,
                "allowed" if failure is None else "denied",
                reason_code=failure.code if failure else None,
                fields={
                    "skill": request.name,
                    "arguments": request.arguments,
                    "action_class": (
                        self.assistant.skills.get(request.name).action_class.value
                        if self.assistant.skills.get(request.name)
                        else None
                    ),
                    "action_authorized": False,
                },
            )
            if failure is not None:
                return self._cloud_failure(
                    trace, normalized, "tool_policy_" + failure.code, route
                )
            execution = self.assistant.skills.execute(
                request.name, request.arguments, administrator=administrator
            )
            if not execution.ok:
                return self._cloud_failure(
                    trace, normalized, "tool_execution_failed", route
                )
            safe_result = _bounded_result(execution.result)
            trace.emit(
                TraceStage.TOOL,
                "complete",
                fields={
                    "skill": request.name,
                    "latency_ms": round(execution.elapsed_seconds * 1000, 3),
                    "result_summary": safe_result,
                },
            )
            previous = turn.response_id
            tool_output = {
                "type": "function_call_output",
                "call_id": request.call_id,
                "output": json.dumps(
                    safe_result, separators=(",", ":"), ensure_ascii=True
                ),
            }
        return self._cloud_failure(trace, normalized, "cloud_request_limit", route)

    def _cloud_failure(
        self, trace: ExecutionTrace, normalized: str, code: str, route: RoutedIntent
    ) -> ServiceResponse:
        trace.emit(TraceStage.MODEL, "failed", reason_code=code)
        local_text = (
            route.message or "I couldn't safely complete the open-ended request."
        )
        if code in {"cloud_disabled", "missing_api_key"}:
            local_text = "I can't answer that type of question with the local skills currently enabled."
        elif code == "budget_denied":
            local_text = (
                "The cloud request was blocked by the configured budget guardrail."
            )
        return ServiceResponse(
            trace.trace_id,
            trace.request_id,
            local_text,
            normalized,
            "local_fallback",
            (code,),
            stopping_reason=code,
        )

    def _from_assistant_result(
        self,
        result: AssistantResponse,
        trace: ExecutionTrace,
        decision: RouteDecision,
    ) -> ServiceResponse:
        model = result.diagnostic.cloud_model if result.diagnostic else None
        effort = result.diagnostic.cloud_reasoning if result.diagnostic else None
        stopping = result.diagnostic.stopping_reason if result.diagnostic else None
        trace.emit(
            TraceStage.RESPONSE,
            "ready",
            fields={
                "response_text": result.response_text,
                "route": decision.selected_route,
                "model_avoided": decision.model_avoided,
            },
        )
        return ServiceResponse(
            trace.trace_id,
            trace.request_id,
            result.response_text,
            result.normalized_text,
            decision.selected_route,
            decision.reason_codes,
            skill=result.route.skill,
            model=model,
            reasoning_effort=effort,
            usage={
                "estimated_cost_usd": result.diagnostic.estimated_cost_usd,
                "tool_calls": result.diagnostic.tool_calls,
            }
            if result.diagnostic
            else None,
            stopping_reason=stopping,
        )

    def _finish(
        self,
        session: BrowserSession,
        trace: ExecutionTrace,
        response: ServiceResponse,
        started: float,
    ) -> ServiceResponse:
        self.sessions.add_message(
            session, "assistant", response.response_text, trace.trace_id
        )
        if not any(event.stage == TraceStage.RESPONSE.value for event in trace.events):
            trace.emit(
                TraceStage.RESPONSE,
                "ready",
                fields={
                    "response_text": response.response_text,
                    "route": response.route,
                },
            )
        wall_seconds = time.perf_counter() - started
        trace.emit(
            TraceStage.COMPLETE,
            "complete",
            reason_code=response.stopping_reason,
            fields={"total_latency_ms": round(wall_seconds * 1000, 3)},
        )
        failed = response.route == "local_fallback" or response.stopping_reason in {
            "budget_denied",
            "cloud_disabled",
            "cloud_request_limit",
            "tool_call_limit",
        }
        self.ledger.record_request(
            request_id=trace.request_id,
            session_id=session.session_id,
            source=trace.source,
            route_category=response.route,
            model=response.model,
            provider="openai" if response.model else None,
            model_avoided=response.model is None,
            wall_seconds=wall_seconds,
            success=not failed,
            error_code=response.stopping_reason if failed else None,
        )
        return response

    def _diagnostic_request(
        self, text: str, override: RouteOverride
    ) -> DiagnosticRequest | None:
        planner = self.assistant.diagnostic_planner
        if planner is None:
            return None
        cloud_allowed = override not in {
            RouteOverride.CLOUD_DISABLED,
            RouteOverride.DETERMINISTIC_LOCAL,
            RouteOverride.LOCAL_DIAGNOSTIC,
        }
        return planner.request_from_text(
            text,
            local_only=not cloud_allowed,
            allow_cloud=cloud_allowed,
            max_escalation=self.settings.cloud.max_escalation_steps + 1,
        )

    @staticmethod
    def _explicit_diagnostic_language(text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "why ",
                "diagnose",
                "diagnostic",
                "troubleshoot",
                "not showing",
                "not working",
                "isn't working",
                "failed",
                "broken",
                "problem",
                "root cause",
            )
        )

    @staticmethod
    def _complexity_features(
        text: str,
        route: RoutedIntent,
        diagnostic: DiagnosticRequest | None,
        override: RouteOverride,
    ) -> dict[str, object]:
        return {
            "deterministic_route_matched": route.matched,
            "complete_required_slots": route.matched and not route.missing_arguments,
            "missing_required_arguments": list(route.missing_arguments),
            "single_operation": len(
                [word for word in (" and ", " also ", " then ") if word in text]
            )
            == 0,
            "historical_data_required": any(
                word in text for word in ("history", "trend", "yesterday", "baseline")
            ),
            "comparison_or_aggregation": route.aggregate
            or any(
                word in text
                for word in ("compare", "most", "highest", "average", "mean")
            ),
            "diagnostic_domain_recognized": diagnostic is not None,
            "open_ended_causal_request": any(
                word in text
                for word in ("why", "might", "causing", "caused", "affected")
            ),
            "external_general_knowledge_required": diagnostic is None
            and not route.matched
            and len(text.split()) > 4,
            "admin_override": override.value
            if override is not RouteOverride.AUTO
            else None,
        }

    def _relevant_skill_tools(
        self, text: str, administrator: bool = False
    ) -> tuple[dict[str, object], ...]:
        names: set[str] = set()
        if any(
            word in text
            for word in (
                "sensor",
                "humidity",
                "temperature",
                "box",
                "co2",
                "air quality",
            )
        ):
            names.update(
                {
                    "get_sensor_value",
                    "compare_sensor_metric",
                    "get_sensor_status",
                    "get_sensor_history_summary",
                }
            )
        if any(
            word in text
            for word in (
                "history",
                "trend",
                "window",
                "compare",
                "spike",
                "correlation",
            )
        ):
            names.update(
                {
                    "get_sensor_history",
                    "summarize_sensor_window",
                    "compare_sensor_windows",
                    "detect_metric_spikes",
                    "correlate_metrics",
                }
            )
        if any(
            word in text
            for word in ("server", "pi", "memory", "load", "disk", "service")
        ):
            names.update(
                {"get_server_health", "get_host_observation", "get_stack_observation"}
            )
        if any(
            word in text
            for word in ("grafana", "mqtt", "influx", "dashboard", "tailscale")
        ):
            names.update({"get_stack_observation", "get_network_observation"})
        if any(word in text for word in ("printer", "x2d", "bambu")):
            if any(word in text for word in ("maintenance", "service", "advisory")):
                names.update(
                    {
                        "get_printer_maintenance",
                        "get_printer_maintenance_events",
                    }
                )
            if any(word in text for word in ("usage", "hours", "heavily", "printed")):
                names.add("get_printer_usage")
            if not names.intersection(
                {
                    "get_printer_usage",
                    "get_printer_maintenance",
                    "get_printer_maintenance_events",
                }
            ):
                names.update(
                    {
                        "get_printer_status",
                        "get_current_print",
                        "get_printer_temperatures",
                        "get_recent_prints",
                        "get_print_details",
                        "analyze_print_environment",
                    }
                )
        if not administrator:
            # A cloud model must never be handed a tool the caller could not
            # have invoked directly.
            names = {
                name
                for name in names
                if not self.assistant.skills.requires_administrator(name)
            }
        catalog = {
            item.name: item for item in self.assistant.llm_tools if item.executable
        }
        tools: list[dict[str, object]] = []
        for name in sorted(names):
            if name in catalog:
                definition = catalog[name]
                tools.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": definition.description,
                        "parameters": definition.parameters,
                        "strict": True,
                    }
                )
                continue
            schema = self._promoted_schema(name)
            spec = self.assistant.skills.get(name)
            if spec is not None and spec.action_class is ActionClass.ACTION:
                # Action requests are frozen and authenticated locally. They
                # are never model-callable tools, even for administrators.
                continue
            if spec is not None and spec.version.startswith("2."):
                schema = spec.input_schema
            if spec is not None and schema is not None:
                tools.append(
                    {
                        "type": "function",
                        "name": name,
                        "description": spec.description,
                        "parameters": schema,
                        "strict": True,
                    }
                )
        return tuple(tools[:6])

    def _prefetch_local_evidence(
        self,
        text: str,
        route: RoutedIntent,
        trace: ExecutionTrace,
        administrator: bool = False,
    ) -> tuple[dict[str, object], ...]:
        candidates: list[tuple[str, dict[str, object]]] = []
        if route.matched and route.skill:
            candidates.append((route.skill, dict(route.arguments)))
        if "humid" in text or "humidity" in text:
            candidates.append(
                (
                    "compare_sensor_metric",
                    {
                        "group": "filament_boxes",
                        "metric": "humidity",
                        "operation": "max",
                    },
                )
            )
        observations: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for name, arguments in candidates[:3]:
            key = (name, json.dumps(arguments, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            failure = self.assistant.skills.validate_proposal(
                name, arguments, administrator=administrator
            )
            if failure is not None:
                trace.emit(
                    TraceStage.POLICY,
                    "denied",
                    reason_code=failure.code,
                    fields={"skill": name, "prefetch": True},
                )
                continue
            execution = self.assistant.skills.execute(
                name, arguments, administrator=administrator
            )
            trace.emit(
                TraceStage.TOOL,
                "prefetch_complete" if execution.ok else "prefetch_failed",
                reason_code=execution.failure.code
                if execution.failure
                else "local_evidence_prefetch",
                fields={
                    "skill": name,
                    "arguments": arguments,
                    "latency_ms": round(execution.elapsed_seconds * 1000, 3),
                    "result_summary": _bounded_result(execution.result),
                },
            )
            if execution.ok:
                observations.append(
                    {
                        "skill": name,
                        "arguments": arguments,
                        "result": _bounded_result(execution.result),
                    }
                )
        return tuple(observations)

    def _promoted_schema(self, name: str) -> dict[str, object] | None:
        entity_ids = [
            item.entity_id
            for item in self.assistant.router.entities.entities
            if item.sensor_type != "printer"
        ]
        values: dict[str, tuple[str, list[str]]] = {
            "get_host_observation": (
                "metric",
                [
                    "uptime",
                    "load",
                    "memory",
                    "swap",
                    "disk",
                    "temperature",
                    "throttle",
                    "failed_units",
                ],
            ),
            "get_stack_observation": (
                "component",
                [
                    "mqtt",
                    "bridge",
                    "dashboard",
                    "influxdb",
                    "grafana",
                    "home_assistant",
                    "services",
                ],
            ),
            "get_network_observation": (
                "view",
                ["interfaces", "routes", "tailscale", "listeners"],
            ),
        }
        if name == "get_sensor_history_summary":
            return _object_schema(
                {
                    "entity": {"type": "string", "enum": entity_ids},
                    "range_key": {"type": "string", "enum": ["1h", "24h", "7d"]},
                },
                ["entity", "range_key"],
            )
        item = values.get(name)
        if item is None:
            return None
        field, allowed = item
        return _object_schema({field: {"type": "string", "enum": allowed}}, [field])

    def _wire_diagnostic_cloud(self) -> None:
        engine = self.assistant.diagnostic_engine
        if engine is None:
            return
        engine.cloud = CloudDiagnosticEscalator(
            OpenAIResponsesReasoner(self.settings.cloud),
            engine.tools,
            self.settings.cloud,
            ledger=self.ledger,
        )

    def _local_tts_engine(self, speed: float):
        from butters.tts.sherpa_engine import SherpaOnnxPiperTTS

        return SherpaOnnxPiperTTS(
            self.settings.tts.model_dir,
            num_threads=self.settings.tts.num_threads,
            speed=speed,
            max_text_chars=self.settings.tts.max_text_chars,
        )

    @staticmethod
    def _unsupported_response(
        raw: str, normalized: str, route: RoutedIntent, code: str
    ) -> AssistantResponse:
        message = route.message or (
            "I can't answer that type of question with the local skills currently enabled."
            if code == "cloud_disabled"
            else "I can't answer that request with the local skills currently enabled."
        )
        return AssistantResponse(
            raw,
            normalized,
            route,
            message,
            0.0,
            routing_path="unsupported",
            policy_status=code,
        )


def _denied_features(normalized: str) -> dict[str, object]:
    """Minimal feature record for a request refused before classification."""

    return {
        "deterministic_route_matched": False,
        "administrator_required": True,
        "word_count": len(normalized.split()),
    }


def _object_schema(
    properties: dict[str, object], required: list[str]
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _bounded_result(value: object) -> object:
    if value is None:
        return None
    candidate = asdict(value) if is_dataclass(value) else value
    clean, _redactions = sanitize_value(candidate, max_text_bytes=2048)
    encoded = json.dumps(clean, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 8192:
        return {"summary": encoded[:8000] + "[TRUNCATED]"}
    return clean


def _direct_desktop_action(text: str) -> bool:
    text = text.casefold()
    return any(
        phrase in text
        for phrase in (
            "turn on my computer",
            "wake my computer",
            "wake the desktop",
        )
    ) and any(word in text for word in ("parsec", "remote"))


def _action_summary(skill: str, arguments: dict[str, object]) -> str:
    labels = {
        "wake_desktop": "Wake the configured desktop",
        "start_remote_desktop_session": "Wake the configured desktop and prepare its remote session",
        "monitors_off": "Turn off the configured desktop monitors through Home Assistant",
        "monitors_on": "Turn on the configured desktop monitors through Home Assistant",
        "lock_desktop": "Lock the configured desktop",
        "sleep_desktop": "Put the configured desktop to sleep",
        "restart_desktop": "Restart the configured desktop",
        "shutdown_desktop": "Shut down the configured desktop",
        "wake_nas": "Wake the configured NAS",
        "restart_butters_service": "Restart the Butters web service",
        "reboot_butters_host": "Reboot the Butters host",
        "shutdown_butters_host": "Shut down the Butters host",
    }
    if skill.startswith("set_"):
        device = skill.removeprefix("set_").replace("_", " ")
        state = arguments.get("state")
        duration = arguments.get("duration_minutes")
        suffix = f" for {duration} minutes" if duration is not None else ""
        return f"Turn {device} {state}{suffix}"
    return labels.get(skill, "Perform the exact registered action")


def _plan_summary(
    steps: tuple[tuple[str, dict[str, object]], ...],
) -> str:
    return " and ".join(_action_summary(skill, arguments) for skill, arguments in steps)
