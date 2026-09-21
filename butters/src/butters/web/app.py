"""Starlette ASGI application for the separate Butters Beta 1 service."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from butters.actions.coordinator import ActionCoordinatorError
from butters.actions.store import ActionStateError
from butters.ai.capabilities import CapabilityError
from butters.ai.credentials import CredentialError
from butters.appearance import AppearanceError
from butters.appearance import stylesheet as appearance_stylesheet
from butters.assistant_config import AssistantSettings, load_assistant_settings
from butters.audio.wav import WavFormatError, measure_wav
from butters.auth.manager import WebAuthnError
from butters.auth.store import AuthStateError
from butters.cloud.provider_accounting import AccountingError
from butters.cloud.usage_admin_credential import UsageAdminCredentialError
from butters.config import default_vocabulary_path, load_stt_settings
from butters.diagnostics.sanitizer import sanitize_text, sanitize_value
from butters.remediation.skill_builder import SkillAuthoringError
from butters.stt.normalization import DomainVocabulary, load_domain_vocabulary
from butters.web.audio import BrowserAudioError, BrowserAudioStream
from butters.web.locality import LocalityClassifier
from butters.web.portal import PortalError, PortalService
from butters.web.security import AuthPolicy, RateLimiter, SecurityError
from butters.web.service import BetaAssistantService, RouteOverride
from butters.web.sessions import BrowserSession, SessionError
from butters.web.speech import SpeechProviderError, VoicePreset
from butters.web.stt_pool import STTEngineLease, STTEnginePool, STTEnginePoolError
from butters.web.trace import TraceStage

LOGGER = logging.getLogger("butters.web")
WEB_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = WEB_ROOT / "static"
# Only non-privileged shared assets are publicly mountable. The HTML documents
# stay outside the mount so /admin cannot be fetched anonymously.
ASSET_ROOT = STATIC_ROOT / "assets"
SESSION_COOKIE = "butters_session"


class SecurityHeadersMiddleware:
    """Small ASGI middleware with no request buffering or task hand-off."""

    _HEADERS = (
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"permissions-policy", b"camera=(), geolocation=(), microphone=(self)"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; "
            b"connect-src 'self'; media-src 'self' blob:; "
            b"img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        ),
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", ()))
                existing = {name.lower() for name, _value in headers}
                headers.extend(
                    (name, value)
                    for name, value in self._HEADERS
                    if name not in existing
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(
    settings: AssistantSettings | None = None,
    vocabulary: DomainVocabulary | None = None,
    service: BetaAssistantService | None = None,
    stt_engine_factory: Any | None = None,
    *,
    trusted_peers: frozenset[str] | None = None,
) -> Starlette:
    # The loader resolves BUTTERS_CONFIG itself, so the application object and
    # the uvicorn listener cannot end up configured by different files.
    configured = settings or load_assistant_settings()
    domain_vocabulary = vocabulary or load_domain_vocabulary(default_vocabulary_path())
    runtime = service or BetaAssistantService(configured, domain_vocabulary)
    auth = AuthPolicy(configured.web, trusted_peers=trusted_peers)
    portal = PortalService(
        runtime,
        configured.portal,
        configured.nas_endpoints,
        LocalityClassifier(
            configured.portal,
            # The portal trusts exactly the peers the rest of the policy
            # trusts for proxy-supplied identity, and nothing else.
            trusted_peers=auth.trusted_peers,
        ),
    )
    portal_wake_rate = RateLimiter(
        rate_per_minute=configured.portal.wake_rate_per_minute,
        burst=configured.portal.wake_burst,
    )
    portal_status_rate = RateLimiter(
        rate_per_minute=configured.portal.status_rate_per_minute,
        burst=configured.portal.status_burst,
    )
    if not configured.web.production_origin_configured:
        LOGGER.warning(
            "web.allowed_origins is empty; mutations and session allocation stay "
            "closed until the private HTTPS origin is configured"
        )
    normal_rate = RateLimiter(rate_per_minute=30, burst=8)
    expensive_rate = RateLimiter(rate_per_minute=6, burst=2)
    admin_rate = RateLimiter(rate_per_minute=20, burst=5)
    session_rate = RateLimiter(
        rate_per_minute=configured.web.session_create_rate_per_minute,
        burst=configured.web.session_create_burst,
    )
    worker_capacity = asyncio.Semaphore(
        configured.web.max_workers + configured.web.max_queued_requests
    )
    voice_slots = asyncio.Semaphore(configured.browser_audio.max_concurrent_sessions)
    voice_capacity = asyncio.Semaphore(
        configured.browser_audio.max_concurrent_sessions
        + configured.browser_audio.max_queue_depth
    )
    worker_pool = ThreadPoolExecutor(
        max_workers=configured.web.max_workers,
        thread_name_prefix="butters-web",
    )
    # Teardown must never queue behind admitted work: releasing a recognizer is
    # what frees the capacity that the admission gate is rationing.
    teardown_pool = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="butters-teardown"
    )
    started = time.monotonic()
    make_stt_engine = stt_engine_factory or _new_stt_engine
    # These small immutable files are loaded once. Starlette's FileResponse
    # and StaticFiles delegate stat/open to AnyIO threads; on this Python
    # 3.13/aarch64 host that path can hit the same executor wake-up failure that
    # run_blocking() already polls around. An explicit allow-list also preserves
    # the invariant that admin/index HTML is never public under /assets.
    index_document = (STATIC_ROOT / "index.html").read_bytes()
    admin_document = (STATIC_ROOT / "admin.html").read_bytes()
    portal_document = (STATIC_ROOT / "portal.html").read_bytes()
    # The stylesheet is authored as a token layer, a base layer, a shared
    # component layer and one layer per surface. Each is named explicitly for
    # the same reason the list existed before: nothing becomes public here by
    # being dropped into a directory.
    public_assets = {
        "tokens.css": (
            (ASSET_ROOT / "tokens.css").read_bytes(),
            "text/css",
        ),
        "base.css": (
            (ASSET_ROOT / "base.css").read_bytes(),
            "text/css",
        ),
        "components.css": (
            (ASSET_ROOT / "components.css").read_bytes(),
            "text/css",
        ),
        "chat.css": (
            (ASSET_ROOT / "chat.css").read_bytes(),
            "text/css",
        ),
        "admin.css": (
            (ASSET_ROOT / "admin.css").read_bytes(),
            "text/css",
        ),
        "portal.css": (
            (ASSET_ROOT / "portal.css").read_bytes(),
            "text/css",
        ),
        "app.js": (
            (ASSET_ROOT / "app.js").read_bytes(),
            "text/javascript",
        ),
        "admin.js": (
            (ASSET_ROOT / "admin.js").read_bytes(),
            "text/javascript",
        ),
        "portal.js": (
            (ASSET_ROOT / "portal.js").read_bytes(),
            "text/javascript",
        ),
    }
    stt_pool = STTEnginePool(
        make_stt_engine,
        max_size=configured.browser_audio.max_concurrent_sessions,
        acquire_timeout_seconds=configured.browser_audio.idle_timeout_seconds,
    )

    async def _await_future(future: Any, timeout: float | None = None) -> Any:
        # Polling avoids a Python 3.13/aarch64 executor wake-up failure seen
        # after worker-side entropy calls, while keeping the event loop free.
        deadline = None if timeout is None else time.monotonic() + timeout
        while not future.done():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("worker did not finish within its deadline")
            await asyncio.sleep(0.005)
        return future.result()

    async def run_blocking(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        invocation = partial(function, *args, **kwargs)
        if not await _acquire(worker_capacity):
            raise SecurityError("queue_full", "assistant worker queue is full", 503)
        future = worker_pool.submit(invocation)
        try:
            return await _await_future(future)
        except asyncio.CancelledError:
            future.cancel()
            raise
        finally:
            worker_capacity.release()

    async def run_teardown(function: Any, /, *args: Any) -> None:
        """Best-effort release that can never fail the caller's cleanup path."""

        try:
            future = teardown_pool.submit(partial(function, *args))
        except RuntimeError:  # pool already shut down
            LOGGER.warning("teardown pool unavailable; releasing without close")
            return
        try:
            await _await_future(future, timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.warning("voice stream teardown failed", exc_info=True)

    async def index(_request: Request) -> Response:
        return Response(index_document, media_type="text/html")

    async def admin_page(request: Request) -> Response:
        _admin(request, auth)
        return Response(admin_document, media_type="text/html")

    async def theme_stylesheet(_request: Request) -> Response:
        """The saved appearance, as a render-blocking stylesheet.

        Every surface links this in <head>, so the browser has the chosen
        colours before it paints and there is no flash of the default palette.
        It is public for the same reason the other stylesheets are: Chat and
        the Portal sign-in page both render before any session exists, and the
        body is nothing but colour values an administrator already picked.

        Generated per request and never cached, so a saved change is visible
        on the next reload of any device.
        """

        try:
            body = appearance_stylesheet(runtime.effective_appearance())
        except AppearanceError:
            # A theme must never be able to stop a page rendering. Falling
            # through to the shipped tokens is a working page, not a broken one.
            body = "/* appearance unavailable; using the built-in palette */\n"
        return Response(
            body,
            media_type="text/css",
            headers={"Cache-Control": "no-store"},
        )

    async def public_asset(request: Request) -> Response:
        asset = public_assets.get(request.path_params["asset_name"])
        if asset is None:
            return Response(status_code=404)
        content, media_type = asset
        return Response(content, media_type=media_type)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "service": "butters", "version": "beta1"})

    async def ready(_request: Request) -> Response:
        """Readiness that actually exercises what it reports.

        ``configuration`` and ``deterministic_router`` used to be the string
        literal "ready", so the endpoint returned 200 for a process whose
        registries were empty or whose router raised on every request, and it
        was barely distinguishable from the static /healthz beside it.

        Gating checks are the ones that make the service unable to answer:
        a state directory it cannot write, an empty registry, a router that
        raises, or an unset production origin. The broker and the optional
        cloud and speech capabilities are reported but never gate, because a
        Butters install is supported without them and a beta install
        deliberately does not provision the broker.
        """

        origin_ready = configured.web.production_origin_configured
        checks = {
            "configuration": _configuration_check(runtime),
            "state_directory": _state_directory_check(runtime.state_dir),
            "deterministic_router": _router_check(runtime),
            "action_broker": _broker_check(configured),
            "cloud_optional": "ready"
            if runtime.general_reasoner.available
            else "disabled",
            "production_origin": "ready" if origin_ready else "unconfigured",
            "local_stt": (
                "ready"
                if stt_pool.stats()["available"]
                else "unavailable"
                if stt_pool.stats()["last_error"]
                else "warming"
            ),
        }
        gating = ("configuration", "state_directory", "deterministic_router")
        healthy = all(checks[name] == "ready" for name in gating) and origin_ready
        status = 200 if healthy else 503
        return JSONResponse(
            {"status": "ready" if healthy else "not_ready", "checks": checks},
            status_code=status,
        )

    async def session_endpoint(request: Request) -> Response:
        try:
            existing = _session_from_request(request, runtime, required=False)
            if existing is None:
                # Admission control happens before any allocation so a flood is
                # refused rather than absorbed, and administrators keep a reserve.
                auth.require_browser_context(request.headers)
                client = request.client.host if request.client else None
                peer = auth.peer_key(request.headers, client)
                if not session_rate.check("session:" + peer):
                    return _error(
                        "rate_limited", "session request rate limit exceeded", 429
                    )
                existing = runtime.sessions.create(
                    peer_key=peer,
                    administrator=auth.is_administrator(request.headers, client),
                )
            else:
                _require_session_peer(request, existing, auth)
            response = JSONResponse(
                {
                    "session": "ready",
                    "csrf_token": existing.csrf_token,
                    "interaction_generation": existing.interaction_generation,
                    "voice_disclosure": runtime.voice_disclosure(),
                    "messages": [
                        {
                            "role": item.role,
                            "text": item.text,
                            "trace_id": item.trace_id,
                        }
                        for item in existing.messages
                    ],
                }
            )
            response.set_cookie(
                SESSION_COOKIE,
                existing.session_id,
                max_age=int(configured.web.session_ttl_seconds),
                httponly=True,
                secure=not configured.web.development_mode,
                samesite="strict",
                path="/",
            )
            return response
        except (SecurityError, SessionError, ValueError) as exc:
            return _exception_response(exc)

    async def clear_session(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            generation = _interaction_generation(
                request.headers.get("x-butters-generation")
            )
            # Clearing now waits on the per-session turn lock, so it must not run
            # on the event loop: an in-flight turn would stall the whole service.
            cleared = await run_blocking(
                runtime.clear_conversation,
                session,
                interaction_generation=generation,
            )
            return JSONResponse(
                {
                    "status": "cleared" if cleared else "superseded",
                    "csrf_token": session.csrf_token,
                }
            )
        except (SecurityError, SessionError, ValueError) as exc:
            return _exception_response(exc)

    async def chat(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
        except (SecurityError, SessionError) as exc:
            # Only this guard can prove the assistant was never entered. Keep
            # the retry semantic attached to the control-flow boundary rather
            # than trusting an error-code string returned from deeper work.
            return _exception_response(
                exc,
                safe_to_retry=isinstance(exc, SessionError)
                and exc.code == "invalid_session",
            )
        try:
            generation = _interaction_generation(
                request.headers.get("x-butters-generation")
            )
            if not normal_rate.check(session.session_id):
                return _error("rate_limited", "request rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            text = payload.get("text")
            if not isinstance(text, str):
                return _error("invalid_request", "text must be a string", 400)
            result = await run_blocking(
                runtime.handle_text,
                session,
                text,
                interaction_generation=generation,
            )
            return JSONResponse(result.as_dict())
        except (SecurityError, SessionError, ValueError, PermissionError) as exc:
            return _exception_response(exc)

    async def conversational_plan(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            if not normal_rate.check("planner:" + session.session_id):
                return _error("rate_limited", "planner rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"text"} or not isinstance(payload["text"], str):
                return _error("invalid_request", "text is required", 400)
            return JSONResponse(
                await run_blocking(
                    runtime.plan_conversation, session, payload["text"]
                )
            )
        except (SecurityError, SessionError, ValueError, PermissionError) as exc:
            return _exception_response(exc)

    async def speech(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            if not expensive_rate.check(session.session_id):
                return _error("rate_limited", "speech rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            trace_id = payload.get("trace_id")
            if not isinstance(trace_id, str):
                return _error("invalid_request", "trace_id is required", 400)
            result = await run_blocking(
                runtime.synthesize_trace_response, session, trace_id
            )
            return Response(
                result.audio_wav,
                media_type="audio/wav",
                headers={
                    "X-Butters-TTS-Provider": result.provider,
                    "X-Butters-Audio-Seconds": _audio_seconds_header(
                        result.audio_seconds, 3
                    ),
                },
            )
        except (SecurityError, SessionError, SpeechProviderError, ValueError) as exc:
            return _exception_response(exc)

    async def overview(request: Request) -> Response:
        try:
            identity = _admin(request, auth)
            return JSONResponse(
                {
                    "service": "butters-beta1",
                    "uptime_seconds": round(time.monotonic() - started, 1),
                    "administrator": identity,
                    "active_sessions": len(runtime.sessions.summaries()),
                    "trace_capacity": configured.web.trace_capacity,
                    "cloud_available": runtime.general_reasoner.available,
                    "local_llm_enabled": configured.llm.enabled,
                    "bind": f"{configured.web.host}:{configured.web.port}",
                    # Passive facets only: no connection id, credential,
                    # capability list, or command surface crosses to Admin.
                    "desktop_agent_state": runtime.desktop_agent.snapshot().safe_dict(),
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def traces(request: Request) -> Response:
        try:
            _admin(request, auth)
            limit = _bounded_query_int(request, "limit", 50, 1, 200)
            return JSONResponse(
                {"traces": runtime.traces.recent(limit, include_text=True)}
            )
        except (SecurityError, ValueError) as exc:
            return _exception_response(exc)

    async def sessions(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse({"sessions": runtime.sessions.summaries()})
        except SecurityError as exc:
            return _exception_response(exc)

    async def routing_test(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            payload = await _json_body(request, configured.web.max_request_bytes)
            text = payload.get("text")
            if not isinstance(text, str):
                return _error("invalid_request", "text must be a string", 400)
            try:
                override = RouteOverride(str(payload.get("override", "auto")))
            except ValueError:
                return _error("invalid_override", "route override is invalid", 400)
            forced_model = payload.get("model")
            if forced_model is not None and not isinstance(forced_model, str):
                return _error("invalid_model", "model must be a string", 400)
            effort = str(payload.get("reasoning_effort", "medium"))
            output_limit = payload.get("max_output_tokens")
            if output_limit is not None and (
                isinstance(output_limit, bool) or not isinstance(output_limit, int)
            ):
                return _error(
                    "invalid_limit", "max_output_tokens must be an integer", 400
                )
            result = await run_blocking(
                runtime.handle_text,
                session,
                text,
                override=override,
                forced_model=forced_model,
                reasoning_effort=effort,
                max_output_tokens=output_limit,
                administrator=True,
            )
            return JSONResponse(result.as_dict())
        except (SecurityError, SessionError, ValueError, PermissionError) as exc:
            return _exception_response(exc)

    async def model_status(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                {
                    "text": {
                        "provider": configured.cloud.provider,
                        "models": list(configured.cloud.pricing),
                        "enabled": configured.cloud.enabled,
                        "allow_paid_calls": configured.cloud.allow_paid_calls,
                        "max_output_tokens": configured.cloud.max_output_tokens,
                        "efforts": [
                            "none",
                            "minimal",
                            "low",
                            "medium",
                            "high",
                            "xhigh",
                            "max",
                        ],
                    },
                    "stt": {
                        "default": configured.providers.stt_default,
                        "providers": ["local", "openai"],
                        "cloud_model": configured.providers.cloud_stt_model,
                        "allow_paid": configured.providers.allow_paid_stt,
                        "local_pool": stt_pool.stats(),
                    },
                    "tts": {
                        "default": configured.providers.tts_default,
                        "providers": ["local", "openai"],
                        "cloud_model": configured.providers.cloud_tts_model,
                        "allow_paid": configured.providers.allow_paid_tts,
                    },
                    "pricing": {
                        "source": configured.cloud.pricing_source,
                        "date": configured.cloud.pricing_date,
                        "unknown_fails_closed": True,
                    },
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def stt_test(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("stt:" + identity):
                return _error("rate_limited", "STT test rate limit exceeded", 429)
            if request.headers.get("content-type", "").split(";", 1)[0] != "audio/wav":
                return _error("invalid_audio", "STT test requires audio/wav", 400)
            limit = 8 * 1024 * 1024
            declared = request.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > limit):
                return _error("audio_limit", "STT audio exceeds the byte limit", 413)
            audio = await _bounded_body(request, limit)
            if not audio or len(audio) > limit:
                return _error("audio_limit", "STT audio exceeds the byte limit", 413)
            try:
                # Measured from the bytes actually uploaded. A streaming WAV
                # declares placeholder chunk sizes, and trusting them here
                # would inflate the duration - which this endpoint charges by.
                measurement = measure_wav(audio)
                if (
                    not measurement.measurable
                    or measurement.sample_width != 2
                    or measurement.channels not in {1, 2}
                    or measurement.sample_rate
                    not in configured.browser_audio.allowed_sample_rates
                ):
                    raise SpeechProviderError(
                        "malformed_audio",
                        "STT test requires allow-listed 16-bit PCM WAV audio",
                    )
                duration = measurement.duration_seconds
            except WavFormatError as exc:
                raise SpeechProviderError(
                    "malformed_audio", "STT test requires a valid PCM WAV"
                ) from exc
            if (
                duration <= 0
                or duration > configured.browser_audio.max_utterance_seconds
            ):
                raise SpeechProviderError(
                    "audio_duration_limit", "STT audio exceeds the duration limit"
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            result = await run_blocking(
                runtime.transcribe_cloud_preview,
                audio,
                duration_seconds=duration,
                session_id=session.session_id,
            )
            return JSONResponse(asdict(result))
        except (SecurityError, SessionError, SpeechProviderError, ValueError) as exc:
            return _exception_response(exc)

    async def skills(request: Request) -> Response:
        try:
            _admin(request, auth)
            query = request.query_params.get("q", "").casefold()[:128]
            items = runtime.assistant.skills.metadata()
            if query:
                items = tuple(
                    item
                    for item in items
                    if query in str(item.get("name", "")).casefold()
                    or query in str(item.get("description", "")).casefold()
                    or query in str(item.get("category", "")).casefold()
                )
            return JSONResponse({"skills": items})
        except SecurityError as exc:
            return _exception_response(exc)

    async def skill_toggle(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            name = payload.get("name")
            enabled = payload.get("enabled")
            if not isinstance(name, str) or not isinstance(enabled, bool):
                return _error("invalid_request", "name and enabled are required", 400)
            runtime.assistant.skills.set_enabled(name, enabled)
            return JSONResponse({"name": name, "enabled": enabled})
        except (SecurityError, SessionError, ValueError) as exc:
            return _exception_response(exc)

    async def skill_test(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            name = payload.get("name")
            arguments = payload.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(
                    "invalid_request", "name and object arguments are required", 400
                )
            execution = await run_blocking(
                partial(runtime.assistant.skills.execute, administrator=True),
                name,
                arguments,
            )
            safe = {
                "skill": execution.skill,
                "ok": execution.ok,
                "action_class": execution.action_class.value
                if execution.action_class
                else None,
                "elapsed_seconds": execution.elapsed_seconds,
                "result": _jsonable(execution.result),
                "failure": asdict(execution.failure) if execution.failure else None,
            }
            return JSONResponse(safe)
        except (SecurityError, SessionError) as exc:
            return _exception_response(exc)

    async def tools(request: Request) -> Response:
        try:
            _admin(request, auth)
            engine = runtime.assistant.diagnostic_engine
            items = (
                []
                if engine is None
                else [
                    {
                        "name": item.name,
                        "description": item.description,
                        "action_class": item.action_class.value,
                        "input_schema": item.input_schema,
                        "output_schema": item.output_schema,
                        "timeout_seconds": item.timeout_seconds,
                        "max_output_bytes": item.max_output_bytes,
                        "sensitivity_behavior": item.sensitivity_behavior,
                    }
                    for item in engine.tools.tools
                ]
            )
            return JSONResponse({"tools": items, "count": len(items)})
        except SecurityError as exc:
            return _exception_response(exc)


    # ===================== Administrator Tools: Desktop =====================
    #
    # One endpoint per reviewed operation. None of them accepts a skill name,
    # action name, host, command, or broker operation: the generic desktop
    # execution path is not restored here.

    async def desktop_tool_status(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(await run_blocking(runtime.desktop_admin_status))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def desktop_tool_ssh_test(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            return JSONResponse(await run_blocking(runtime.desktop_ssh_test))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def desktop_tool_wake(request: Request) -> Response:
        return await _admin_fixed_action(
            request, runtime.start_admin_desktop_wake, "desktop wake"
        )

    async def desktop_tool_shutdown(request: Request) -> Response:
        return await _admin_fixed_action(
            request, runtime.start_admin_desktop_shutdown, "desktop shutdown"
        )

    async def desktop_tool_streaming(request: Request) -> Response:
        return await _admin_fixed_action(
            request, runtime.start_admin_desktop_streaming, "streaming preparation"
        )

    async def desktop_tool_launch(request: Request) -> Response:
        """Launch one symbolic application through the registered Slice 3 action.

        The only caller-supplied value is `app`, a symbolic identifier. It is
        matched against the registered schema and then resolved by the agent
        against its own local allowlist; no executable path or command line
        exists on this side of the boundary.
        """

        try:
            _admin_mutation(request, runtime, auth)
            session = _bound_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"app"}:
                raise ValueError("launch accepts only an app identifier")
            return JSONResponse(
                await run_blocking(
                    runtime.start_admin_desktop_app_launch,
                    session,
                    _string_field(payload, "app"),
                )
            )
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    # ======================= Administrator Tools: NAS =======================

    async def nas_tool_status(request: Request) -> Response:
        try:
            _admin(request, auth)
            refresh = request.query_params.get("refresh") == "1"
            return JSONResponse(
                await run_blocking(runtime.nas_admin_status, refresh=refresh)
            )
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def wake_nas_tool(request: Request) -> Response:
        return await _admin_fixed_action(
            request, runtime.start_admin_nas_wake, "NAS wake"
        )

    async def shutdown_nas_tool(request: Request) -> Response:
        """Start the fixed NAS shutdown after an explicit confirmation flag.

        The body carries one boolean and nothing else. No IP, host, username,
        remote command, shell, argv, API URL, API token, or shutdown argument
        can be expressed in this request model at all.
        """

        try:
            _admin_mutation(request, runtime, auth)
            session = _bound_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"confirm"}:
                raise ValueError("NAS shutdown accepts only an explicit confirmation")
            return JSONResponse(
                await run_blocking(
                    runtime.start_admin_nas_shutdown,
                    session,
                    confirmed=payload["confirm"] is True,
                )
            )
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def _admin_fixed_action(
        request: Request, starter: Any, label: str
    ) -> Response:
        """Shared shape for the parameterless administrator tool actions."""

        try:
            _admin_mutation(request, runtime, auth)
            session = _bound_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if payload:
                raise ValueError(f"{label} accepts no parameters")
            return JSONResponse(await run_blocking(starter, session))
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    # ================ Administrator: portal enrollment / RBAC ================

    async def portal_identities(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(await run_blocking(portal.identities))
        except (SecurityError, SessionError, PortalError) as exc:
            return _exception_response(exc)

    async def portal_invite(request: Request) -> Response:
        """Mint one single-use enrollment authorization for a named identity."""

        try:
            _admin_mutation(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"identity", "label"}:
                raise ValueError("an invitation names one identity and one label")
            return JSONResponse(
                await run_blocking(
                    portal.create_invite,
                    _string_field(payload, "identity"),
                    _string_field(payload, "label"),
                )
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)

    async def portal_revoke(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"identity"}:
                raise ValueError("revocation names one identity")
            return JSONResponse(
                await run_blocking(
                    portal.revoke_identity, _string_field(payload, "identity")
                )
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)

    # ========================= Partner access portal =========================

    async def portal_page(_request: Request) -> Response:
        if not configured.portal.enabled:
            return Response(status_code=404)
        return Response(portal_document, media_type="text/html")

    async def portal_status(request: Request) -> Response:
        try:
            session = _bound_session(request, runtime, auth)
            return JSONResponse(portal.status(session))
        except (SecurityError, SessionError, PortalError) as exc:
            return _exception_response(exc)

    async def portal_auth_options(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            await _json_body(request, configured.web.max_request_bytes)
            return JSONResponse(
                {"value": await run_blocking(portal.begin_authentication, session)}
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)
        except WebAuthnError as exc:
            return _exception_response(exc)

    async def portal_auth_verify(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            ceremony_id, credential = _ceremony_body(payload)
            return JSONResponse(
                {
                    "value": await run_blocking(
                        portal.finish_authentication,
                        session,
                        ceremony_id=ceremony_id,
                        credential=credential,
                    )
                }
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)
        except WebAuthnError as exc:
            return _exception_response(exc)

    async def portal_register_options(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"label", "invite_token"}:
                raise ValueError("enrollment requires a label and an invitation")
            return JSONResponse(
                {
                    "value": await run_blocking(
                        portal.begin_registration,
                        session,
                        label=_string_field(payload, "label"),
                        invite_token=_string_field(payload, "invite_token"),
                    )
                }
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)
        except WebAuthnError as exc:
            return _exception_response(exc)

    async def portal_register_verify(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            ceremony_id, credential = _ceremony_body(payload)
            return JSONResponse(
                {
                    "value": await run_blocking(
                        portal.finish_registration,
                        session,
                        ceremony_id=ceremony_id,
                        credential=credential,
                    )
                }
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)
        except WebAuthnError as exc:
            return _exception_response(exc)

    async def portal_sign_out(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            return JSONResponse(portal.sign_out(session))
        except (SecurityError, SessionError, PortalError) as exc:
            return _exception_response(exc)

    async def portal_nas_status(request: Request) -> Response:
        try:
            session = _bound_session(request, runtime, auth)
            if not portal_status_rate.check("portal-status:" + session.peer_key):
                return _error("rate_limited", "status rate limit exceeded", 429)
            refresh = request.query_params.get("refresh") == "1"
            return JSONResponse(
                await run_blocking(portal.nas_state, session, refresh=refresh)
            )
        except (SecurityError, SessionError, PortalError) as exc:
            return _exception_response(exc)

    async def portal_wake(request: Request) -> Response:
        """POST only, authenticated, CSRF-checked, same-origin, rate-limited.

        Wake is never reachable by GET, so a prefetch, crawler, link preview,
        `<img>` tag, or plain link cannot send a magic packet. `_mutation_session`
        additionally requires the allow-listed HTTPS Origin and the per-session
        CSRF header, so a cross-origin form POST cannot reach it either.
        """

        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if payload:
                raise ValueError("wake accepts no parameters")
            if not portal_wake_rate.check("portal-wake:" + session.peer_key):
                return _error("rate_limited", "wake rate limit exceeded", 429)
            return JSONResponse(await run_blocking(portal.wake, session))
        except (
            SecurityError,
            SessionError,
            PortalError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def portal_shutdown_plan(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"confirm"}:
                raise ValueError("shutdown accepts only an explicit confirmation")
            return JSONResponse(
                await run_blocking(
                    portal.prepare_shutdown,
                    session,
                    confirmed=payload.get("confirm") is True,
                )
            )
        except (
            SecurityError,
            SessionError,
            PortalError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def portal_shutdown_auth_options(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"pending_action_id"}:
                raise ValueError("pending_action_id is required")
            return JSONResponse(
                {
                    "value": await run_blocking(
                        portal.begin_shutdown_authentication,
                        session,
                        pending_action_id=_string_field(payload, "pending_action_id"),
                    )
                }
            )
        except (SecurityError, SessionError, PortalError, ValueError) as exc:
            return _exception_response(exc)

    async def portal_shutdown_auth_verify(request: Request) -> Response:
        try:
            session = _mutation_session(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) != {"ceremony_id", "credential"}:
                raise ValueError("ceremony_id and credential are required")
            ceremony_id, credential = _ceremony_body(payload)
            return JSONResponse(
                {
                    "value": await run_blocking(
                        portal.finish_shutdown_authentication,
                        session,
                        ceremony_id=ceremony_id,
                        credential=credential,
                    )
                }
            )
        except (
            SecurityError,
            SessionError,
            PortalError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)
        except WebAuthnError as exc:
            return _exception_response(exc)

    async def portal_destination(request: Request) -> Response:
        """Report the server-chosen Jellyfin URL, or nothing at all.

        Query parameters are ignored entirely. `redirect=`, `host=`, `ip=`,
        `local=true`, `remote=true` and any other override are not read here,
        and the returned URL is always one of the two configured destinations.
        """

        try:
            session = _bound_session(request, runtime, auth)
            client = request.client.host if request.client else None
            return JSONResponse(
                await run_blocking(
                    portal.destination, session, request.headers, client
                )
            )
        except (SecurityError, SessionError, PortalError) as exc:
            return _exception_response(exc)

    async def usage(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _session_from_request(request, runtime, required=False)
            # Bounded SQL only, and off the event loop: the retained ledger can
            # hold tens of thousands of rows.
            return JSONResponse(
                await run_blocking(
                    runtime.usage_report,
                    session.session_id if session else None,
                )
            )
        except (SecurityError, SessionError) as exc:
            return _exception_response(exc)

    async def security_status(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                {
                    "credentials": runtime.credential_status(),
                    "backend_loopback_only": configured.web.host
                    in {"127.0.0.1", "::1", "localhost"},
                    "trusted_tailscale_proxy": configured.web.trusted_tailscale_proxy,
                    "admin_allowlist_configured": bool(auth.admin_identities),
                    "production_origin_configured": configured.web.production_origin_configured,
                    "allowed_origin_count": len(configured.web.allowed_origins),
                    "secrets_returned": False,
                    "codex_inherits_environment": False,
                    "codex_execution": runtime.skill_builder.execution_status(),
                    "repository_inspection": runtime.repository_status(),
                    "audio_persisted": False,
                    "transcripts_persisted": False,
                    "trace_ttl_seconds": configured.web.trace_ttl_seconds,
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def auth_status(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            assert session is not None
            return JSONResponse(runtime.authentication_status(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def auth_options(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            purpose = payload.get("purpose", "elevation")
            pending = payload.get("pending_action_id")
            subject = payload.get("subject")
            if not isinstance(purpose, str):
                raise ValueError("purpose must be a string")
            if pending is not None and not isinstance(pending, str):
                raise ValueError("pending_action_id must be a string")
            if subject is not None and not isinstance(subject, str):
                raise ValueError("subject must be a string")
            return JSONResponse(
                await run_blocking(
                    runtime.begin_authentication,
                    session,
                    purpose=purpose,
                    pending_action_id=pending,
                    subject=subject,
                )
            )
        except (
            SecurityError,
            SessionError,
            WebAuthnError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def auth_verify(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            ceremony_id = payload.get("ceremony_id")
            credential = payload.get("credential")
            if not isinstance(ceremony_id, str) or not isinstance(credential, dict):
                raise ValueError("ceremony_id and credential are required")
            return JSONResponse(
                await run_blocking(
                    runtime.finish_authentication,
                    session,
                    ceremony_id=ceremony_id,
                    credential=credential,
                )
            )
        except (
            SecurityError,
            SessionError,
            WebAuthnError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def auth_lock(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            return JSONResponse(runtime.lock_elevation(session))
        except (SecurityError, SessionError) as exc:
            return _exception_response(exc)

    async def passkeys(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            assert session is not None
            return JSONResponse({"credentials": runtime.passkey_credentials(session)})
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def passkey_registration_options(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            label = payload.get("label")
            bootstrap = payload.get("bootstrap_token")
            grant = payload.get("fresh_grant")
            if not isinstance(label, str):
                raise ValueError("label is required")
            if bootstrap is not None and not isinstance(bootstrap, str):
                raise ValueError("bootstrap_token is invalid")
            if grant is not None and not isinstance(grant, str):
                raise ValueError("fresh_grant is invalid")
            return JSONResponse(
                await run_blocking(
                    runtime.begin_passkey_registration,
                    session,
                    label=label,
                    bootstrap_token=bootstrap,
                    fresh_grant=grant,
                )
            )
        except (
            SecurityError,
            SessionError,
            WebAuthnError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def passkey_registration_verify(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            ceremony_id = payload.get("ceremony_id")
            credential = payload.get("credential")
            if not isinstance(ceremony_id, str) or not isinstance(credential, dict):
                raise ValueError("ceremony_id and credential are required")
            return JSONResponse(
                await run_blocking(
                    runtime.finish_passkey_registration,
                    session,
                    ceremony_id=ceremony_id,
                    credential=credential,
                )
            )
        except (
            SecurityError,
            SessionError,
            WebAuthnError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def passkey_label(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            record_id = payload.get("record_id")
            label = payload.get("label")
            if not isinstance(record_id, str) or not isinstance(label, str):
                raise ValueError("record_id and label are required")
            await run_blocking(runtime.label_passkey, session, record_id, label)
            return JSONResponse({"updated": True})
        except (
            SecurityError,
            SessionError,
            AuthStateError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def passkey_revoke(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            record_id = payload.get("record_id")
            fresh_grant = payload.get("fresh_grant")
            if not isinstance(record_id, str) or not isinstance(fresh_grant, str):
                raise ValueError("record_id and fresh_grant are required")
            await run_blocking(runtime.revoke_passkey, session, record_id, fresh_grant)
            return JSONResponse({"revoked": True})
        except (
            SecurityError,
            SessionError,
            AuthStateError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def action_job(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            assert session is not None
            return JSONResponse(
                runtime.action_job(session, request.path_params["job_id"])
            )
        except (SecurityError, SessionError, ActionStateError) as exc:
            return _exception_response(exc)

    async def cancel_action_job(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            return JSONResponse(
                runtime.cancel_action_job(session, request.path_params["job_id"])
            )
        except (
            SecurityError,
            SessionError,
            ActionStateError,
            ActionCoordinatorError,
        ) as exc:
            return _exception_response(exc)

    async def cancel_pending_action(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            runtime.cancel_pending_action(
                session, request.path_params["pending_action_id"]
            )
            return JSONResponse({"cancelled": True})
        except (
            SecurityError,
            SessionError,
            ActionStateError,
            ActionCoordinatorError,
        ) as exc:
            return _exception_response(exc)

    async def capabilities(request: Request) -> Response:
        try:
            session = _bound_session(request, runtime, auth)
            return JSONResponse(
                {
                    "capabilities": runtime.capability_status(
                        administrator=session.administrator
                    )
                }
            )
        except (SecurityError, SessionError) as exc:
            return _exception_response(exc)

    async def admin_actions(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            jobs = runtime.action_state.jobs(
                identity=session.peer_key,
                limit=50,
            )
            return JSONResponse(
                {
                    "jobs": jobs,
                    "audit": runtime.action_state.audit_entries(100),
                    "broker": runtime.assistant.skills.get(
                        "get_action_broker_status"
                    ).metadata()
                    if runtime.assistant.skills.get("get_action_broker_status")
                    else {"available": False},
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def voice_presets(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                {
                    "presets": runtime.voice_presets.as_dicts(),
                    "default": asdict(runtime.voice_presets.default(configured)),
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def save_voice_preset(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            preset = VoicePreset(
                str(payload.get("name", "")),
                str(payload.get("provider", "")),
                str(payload.get("model", "")),
                str(payload.get("voice", "")),
                float(payload.get("speed", 1.0)),
                str(payload.get("instructions", "")),
            )
            saved = runtime.voice_presets.save(
                preset, make_default=payload.get("make_default") is True
            )
            return JSONResponse(asdict(saved))
        except (
            SecurityError,
            SessionError,
            SpeechProviderError,
            ValueError,
            TypeError,
        ) as exc:
            return _exception_response(exc)

    async def preview_voice(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            session = _session_from_request(request, runtime)
            assert session is not None
            if not expensive_rate.check("admin:" + identity):
                return _error("rate_limited", "speech preview rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            phrase = payload.get("phrase")
            if not isinstance(phrase, str) or not 1 <= len(phrase) <= 500:
                return _error(
                    "invalid_phrase", "preview phrase must be 1 to 500 characters", 400
                )
            preset = VoicePreset(
                str(payload.get("name", "preview")),
                str(payload.get("provider", "local")),
                str(payload.get("model", "local-piper")),
                str(payload.get("voice", "kathleen")),
                float(payload.get("speed", 1.0)),
                str(payload.get("instructions", "")),
            )
            result = await run_blocking(
                runtime.synthesize_preview,
                phrase,
                preset,
                session_id=session.session_id,
            )
            return Response(
                result.audio_wav,
                media_type="audio/wav",
                headers={
                    "X-Butters-TTS-Provider": result.provider,
                    "X-Butters-Generation-Seconds": str(
                        round(result.generation_seconds, 4)
                    ),
                    "X-Butters-Audio-Seconds": _audio_seconds_header(
                        result.audio_seconds, 4
                    ),
                    "X-Butters-Cost-USD": "unknown"
                    if result.estimated_cost_usd is None
                    else str(result.estimated_cost_usd),
                },
            )
        except (
            SecurityError,
            SessionError,
            SpeechProviderError,
            ValueError,
            TypeError,
        ) as exc:
            return _exception_response(exc)

    # ------------------- Admin: Integrations (OpenAI) / AI -------------------
    #
    # The candidate API key arrives in exactly one place: the JSON body of an
    # authenticated, same-origin, CSRF-checked HTTPS POST. It is never a path
    # parameter, never a query parameter, and never returned. The response
    # carries state - configured, validated, effective - and nothing else.

    async def ai_catalog(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            return JSONResponse(runtime.ai_catalog(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def appearance_settings(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            return JSONResponse(runtime.appearance_settings(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def apply_appearance(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            # A preview resolves and validates exactly as a save does, and
            # stores nothing, so what the page shows is what a save would keep.
            apply = (
                runtime.preview_appearance
                if request.query_params.get("preview") == "1"
                else runtime.apply_appearance
            )
            return JSONResponse(await run_blocking(apply, session, payload))
        except AppearanceError as exc:
            return _error(exc.code, exc.message, 400)
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    # ---- provider-reported accounting (organization Admin key) ----------

    async def usage_admin_state(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            return JSONResponse(runtime.usage_admin_state(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def usage_admin_sync(request: Request) -> Response:
        """Manual refresh. Rate limited, and it never raises upstream failure."""

        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("accounting:" + identity):
                return _error("rate_limited", "sync rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            return JSONResponse(
                await run_blocking(
                    runtime.sync_provider_accounting, session, force=True
                )
            )
        except (SecurityError, SessionError, ActionCoordinatorError, ValueError) as exc:
            return _exception_response(exc)

    async def usage_admin_test(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("accounting:" + identity):
                return _error("rate_limited", "sync rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            return JSONResponse(
                await run_blocking(runtime.test_usage_admin_credential, session)
            )
        except AccountingError as exc:
            return _error(exc.code, exc.message, 400)
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def usage_admin_key_set(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("credential:" + identity):
                return _error(
                    "rate_limited", "credential change rate limit exceeded", 429
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) - {"admin_api_key", "fresh_grant", "confirm"}:
                raise ValueError("request contains unsupported fields")
            return JSONResponse(
                await run_blocking(
                    runtime.set_usage_admin_credential,
                    session,
                    candidate=payload.get("admin_api_key"),
                    fresh_grant=payload.get("fresh_grant"),
                    confirmed=payload.get("confirm") is True,
                )
            )
        except AccountingError as exc:
            return _error(exc.code, exc.message, 400)
        except (
            SecurityError,
            SessionError,
            UsageAdminCredentialError,
            ActionCoordinatorError,
            AuthStateError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def usage_admin_key_remove(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("credential:" + identity):
                return _error(
                    "rate_limited", "credential change rate limit exceeded", 429
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) - {"fresh_grant", "confirm"}:
                raise ValueError("request contains unsupported fields")
            return JSONResponse(
                await run_blocking(
                    runtime.remove_usage_admin_credential,
                    session,
                    fresh_grant=payload.get("fresh_grant"),
                    confirmed=payload.get("confirm") is True,
                )
            )
        except (
            SecurityError,
            SessionError,
            UsageAdminCredentialError,
            ActionCoordinatorError,
            AuthStateError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def usage_admin_project(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) - {"project_id"}:
                raise ValueError("request contains unsupported fields")
            return JSONResponse(
                await run_blocking(
                    runtime.set_usage_reporting_project, session, payload.get("project_id")
                )
            )
        except AccountingError as exc:
            return _error(exc.code, exc.message, 400)
        except (
            SecurityError,
            SessionError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def ai_settings(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            return JSONResponse(runtime.ai_settings(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def ai_chat_settings(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            return JSONResponse(
                await run_blocking(runtime.apply_chat_settings, session, payload)
            )
        except (
            SecurityError,
            SessionError,
            CapabilityError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def ai_tts_settings(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not admin_rate.check(identity):
                return _error("rate_limited", "administrator rate limit exceeded", 429)
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            return JSONResponse(
                await run_blocking(runtime.apply_speech_settings, session, payload)
            )
        except (
            SecurityError,
            SessionError,
            CapabilityError,
            ActionCoordinatorError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def openai_credential(request: Request) -> Response:
        try:
            _admin(request, auth)
            session = _bound_session(request, runtime, auth)
            return JSONResponse(runtime.openai_credential_state(session))
        except (SecurityError, SessionError, ActionCoordinatorError) as exc:
            return _exception_response(exc)

    async def openai_credential_test(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("credential:" + identity):
                return _error(
                    "rate_limited", "credential test rate limit exceeded", 429
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            return JSONResponse(
                await run_blocking(runtime.test_openai_credential, session)
            )
        except (
            SecurityError,
            SessionError,
            CredentialError,
            ActionCoordinatorError,
            AuthStateError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def openai_credential_set(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("credential:" + identity):
                return _error(
                    "rate_limited", "credential change rate limit exceeded", 429
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) - {"api_key", "fresh_grant", "confirm"}:
                raise ValueError("request contains unsupported fields")
            return JSONResponse(
                await run_blocking(
                    runtime.set_openai_credential,
                    session,
                    candidate=payload.get("api_key"),
                    fresh_grant=payload.get("fresh_grant"),
                    confirmed=payload.get("confirm") is True,
                )
            )
        except (
            SecurityError,
            SessionError,
            CredentialError,
            ActionCoordinatorError,
            AuthStateError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def openai_credential_remove(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("credential:" + identity):
                return _error(
                    "rate_limited", "credential change rate limit exceeded", 429
                )
            session = _session_from_request(request, runtime)
            assert session is not None
            payload = await _json_body(request, configured.web.max_request_bytes)
            if set(payload) - {"fresh_grant", "confirm"}:
                raise ValueError("request contains unsupported fields")
            return JSONResponse(
                await run_blocking(
                    runtime.remove_openai_credential,
                    session,
                    fresh_grant=payload.get("fresh_grant"),
                    confirmed=payload.get("confirm") is True,
                )
            )
        except (
            SecurityError,
            SessionError,
            CredentialError,
            ActionCoordinatorError,
            AuthStateError,
            ValueError,
        ) as exc:
            return _exception_response(exc)

    async def system_info(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                {
                    "state_dir": str(runtime.state_dir),
                    "usage_db": str(runtime.ledger.database_path),
                    "repository_inspection": runtime.repository_status(),
                    "jobs_dir": str(configured.remediation.jobs_dir),
                    "web_workers": configured.web.max_workers,
                    "queue_depth": configured.web.max_queued_requests,
                    "voice_concurrency": configured.browser_audio.max_concurrent_sessions,
                    "sessions": runtime.sessions.capacity(),
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def codex_jobs(request: Request) -> Response:
        try:
            _admin(request, auth)
            jobs = runtime.skill_builder.list(50)
            return JSONResponse(
                {
                    "jobs": [item.as_dict(include_diff=False) for item in jobs],
                    "execution": runtime.skill_builder.execution_status(),
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def codex_job_detail(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                runtime.skill_builder.require(request.path_params["job_id"]).as_dict()
            )
        except (SecurityError, SkillAuthoringError) as exc:
            return _exception_response(exc)

    async def create_skill_job(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("codex:" + identity):
                return _error("rate_limited", "Codex job rate limit exceeded", 429)
            payload = await _json_body(request, configured.web.max_request_bytes)
            description = payload.get("description")
            if not isinstance(description, str):
                return _error("invalid_request", "description must be a string", 400)
            job = await run_blocking(runtime.skill_builder.submit, description)
            return JSONResponse(job.as_dict(), status_code=202)
        except (SecurityError, SessionError, SkillAuthoringError) as exc:
            return _exception_response(exc)

    async def run_skill_job(request: Request) -> Response:
        try:
            identity = _admin_mutation(request, runtime, auth)
            if not expensive_rate.check("codex-run:" + identity):
                return _error(
                    "rate_limited", "Codex execution rate limit exceeded", 429
                )
            job = await run_blocking(
                runtime.skill_builder.run, request.path_params["job_id"]
            )
            return JSONResponse(job.as_dict())
        except (SecurityError, SessionError, SkillAuthoringError) as exc:
            return _exception_response(exc)

    async def decide_skill_job(request: Request) -> Response:
        try:
            _admin_mutation(request, runtime, auth)
            payload = await _json_body(request, configured.web.max_request_bytes)
            decision = payload.get("decision")
            if decision == "approve":
                job = await run_blocking(
                    runtime.skill_builder.approve, request.path_params["job_id"]
                )
            elif decision == "reject":
                job = await run_blocking(
                    runtime.skill_builder.reject, request.path_params["job_id"]
                )
            else:
                return _error(
                    "invalid_decision", "decision must be approve or reject", 400
                )
            return JSONResponse(job.as_dict())
        except (SecurityError, SessionError, SkillAuthoringError) as exc:
            return _exception_response(exc)

    async def logs(request: Request) -> Response:
        try:
            _admin(request, auth)
            return JSONResponse(
                {
                    "events": runtime.traces.recent(50, include_text=False),
                    "persistent_transcripts": False,
                }
            )
        except SecurityError as exc:
            return _exception_response(exc)

    async def voice_socket(websocket: WebSocket) -> None:
        session: BrowserSession | None = None
        stream: BrowserAudioStream | None = None
        engine_lease: STTEngineLease | None = None
        engine_reusable = True
        acquired = False
        capacity_acquired = False
        trace = None
        try:
            auth.require_origin(websocket.headers, websocket.headers.get("host"))
            session = runtime.sessions.require(websocket.cookies.get(SESSION_COOKIE))
            # Identity is bound before the handshake is accepted, so a copied
            # cookie never reaches the rate limiter, a voice slot, the STT pool,
            # a recognizer, the assistant, or the stored conversation.
            _require_peer_identity(
                websocket.headers,
                websocket.client.host if websocket.client else None,
                session,
                auth,
            )
            await websocket.accept()
            first = await asyncio.wait_for(websocket.receive(), timeout=5.0)
            if first.get("type") != "websocket.receive" or not isinstance(
                first.get("text"), str
            ):
                raise BrowserAudioError(
                    "protocol_error", "first WebSocket message must be JSON start"
                )
            try:
                start_message = json.loads(first["text"])
            except json.JSONDecodeError as exc:
                raise BrowserAudioError(
                    "protocol_error", "start message is invalid JSON"
                ) from exc
            if (
                not isinstance(start_message, dict)
                or start_message.get("type") != "start"
            ):
                raise BrowserAudioError(
                    "protocol_error", "first message must have type start"
                )
            interaction_generation = _interaction_generation(
                start_message.get("interaction_generation")
            )
            AuthPolicy.require_csrf(session, start_message.get("csrf_token"))
            if not normal_rate.check("voice:" + session.session_id, cost=2):
                raise BrowserAudioError("rate_limited", "voice rate limit exceeded")
            capacity_acquired = await _acquire(voice_capacity)
            if not capacity_acquired:
                raise BrowserAudioError("voice_capacity", "voice queue is full")
            try:
                await asyncio.wait_for(
                    voice_slots.acquire(),
                    timeout=configured.browser_audio.idle_timeout_seconds,
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                raise BrowserAudioError(
                    "voice_queue_timeout", "voice queue wait timed out"
                ) from exc
            trace = runtime.traces.start(session.session_id, "voice")
            trace.emit(
                TraceStage.AUDIO,
                "stream_started",
                fields={
                    "sample_rate": start_message.get("sample_rate"),
                    "channels": start_message.get("channels"),
                    "encoding": start_message.get("encoding"),
                    "provider": "local",
                    "client_permission_ms": _bounded_client_ms(
                        start_message.get("client_permission_ms")
                    ),
                    "client_setup_ms": _bounded_client_ms(
                        start_message.get("client_setup_ms")
                    ),
                },
            )
            engine_lease = await run_blocking(stt_pool.acquire)
            trace.emit(
                TraceStage.STT,
                "model_ready",
                fields={
                    "provider": "local",
                    "backend": "sherpa-onnx",
                    "accelerator": "cpu",
                    "reused": engine_lease.reused,
                    "engine_acquire_latency_ms": round(
                        engine_lease.acquire_seconds * 1000, 3
                    ),
                    "model_initialization_ms": round(
                        (
                            0.0
                            if engine_lease.reused
                            else engine_lease.initialization_seconds
                        )
                        * 1000,
                        3,
                    ),
                    "cold_model_initialization_ms": round(
                        engine_lease.initialization_seconds * 1000, 3
                    ),
                    "pool": stt_pool.stats(),
                },
            )
            stream = BrowserAudioStream(
                engine_lease.engine,
                domain_vocabulary,
                configured.browser_audio,
            )
            events = await run_blocking(
                stream.start,
                sample_rate=start_message.get("sample_rate"),
                channels=start_message.get("channels"),
                encoding=start_message.get("encoding"),
            )
            await _send_audio_events(websocket, events, trace)
            while True:
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=configured.browser_audio.idle_timeout_seconds,
                )
                if message.get("type") == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if isinstance(data, bytes):
                    events = await run_blocking(stream.accept, data)
                    await _send_audio_events(websocket, events, trace)
                    continue
                raw_control = message.get("text")
                if not isinstance(raw_control, str):
                    raise BrowserAudioError(
                        "protocol_error", "WebSocket frame type is invalid"
                    )
                try:
                    control = json.loads(raw_control)
                except json.JSONDecodeError as exc:
                    raise BrowserAudioError(
                        "protocol_error", "control frame is invalid JSON"
                    ) from exc
                if not isinstance(control, dict):
                    raise BrowserAudioError(
                        "protocol_error", "control frame must be an object"
                    )
                if control.get("type") == "cancel":
                    stream.abort()
                    trace.emit(
                        TraceStage.AUDIO, "cancelled", reason_code="client_cancelled"
                    )
                    await websocket.send_json({"type": "cancelled"})
                    break
                if control.get("type") != "stop":
                    raise BrowserAudioError("protocol_error", "unknown control frame")
                endpoint_reason = control.get("endpoint_reason", "tap")
                if endpoint_reason not in {"tap", "maximum_duration", "pointer_cancel"}:
                    endpoint_reason = "tap"
                trace.emit(
                    TraceStage.AUDIO,
                    "capture_complete",
                    fields={
                        "endpoint_reason": endpoint_reason,
                        "client_capture_ms": _bounded_client_ms(
                            control.get("client_capture_ms")
                        ),
                        "received_audio_seconds": round(stream.audio_seconds, 3),
                    },
                )
                server_final_started = time.perf_counter()
                final = await run_blocking(
                    stream.finish,
                    endpoint_reason="tap_to_record_" + endpoint_reason,
                )
                server_stop_to_final_ms = (
                    time.perf_counter() - server_final_started
                ) * 1000
                assert final.result is not None
                semantic = runtime.assistant.preview_route(final.result.normalized)
                semantic_status = (
                    "complete"
                    if semantic.matched
                    else "incomplete"
                    if semantic.incomplete
                    else "unrecognized"
                )
                utterance = replace(final.result, semantic_status=semantic_status)
                trace.emit(
                    TraceStage.STT,
                    "final",
                    fields={
                        "raw_text": utterance.raw,
                        "normalized_text": utterance.normalized,
                        "endpoint_reason": utterance.endpoint_reason,
                        "processing_latency_ms": round(
                            utterance.processing_seconds * 1000, 3
                        ),
                        "audio_preprocessing_ms": round(
                            utterance.preprocessing_seconds * 1000, 3
                        ),
                        "streaming_inference_ms": round(
                            utterance.inference_seconds * 1000, 3
                        ),
                        "finalization_latency_ms": round(
                            utterance.finalization_latency_seconds * 1000, 3
                        ),
                        "server_stop_to_final_ms": round(server_stop_to_final_ms, 3),
                        "audio_seconds": round(utterance.audio_seconds, 3),
                        "real_time_factor": round(
                            utterance.inference_seconds
                            / max(utterance.audio_seconds, 1e-9),
                            4,
                        ),
                        "semantic_status": semantic_status,
                        "provider": "local",
                    },
                )
                await websocket.send_json(
                    {
                        "type": "final",
                        "raw_text": utterance.raw,
                        "normalized_text": utterance.normalized,
                        "endpoint_reason": utterance.endpoint_reason,
                        "semantic_status": semantic_status,
                        "processing_seconds": utterance.processing_seconds,
                        "finalization_latency_seconds": utterance.finalization_latency_seconds,
                    }
                )
                if not utterance.normalized:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "empty_transcript",
                            "message": "No speech was recognized.",
                        }
                    )
                    break
                # Cancellation stays possible after finalization. The routing
                # turn carries a cooperative token, and a cancel frame that
                # arrives while it runs sets that token. Whether it takes effect
                # depends on how far the turn has got: the service checks it
                # before any tool starts and between compound reads, so work
                # that has not begun is genuinely stopped, while a tool already
                # running may still finish. The client is told which happened
                # rather than being promised a cancellation that did not occur.
                cancel_event = threading.Event()
                routing = asyncio.ensure_future(
                    run_blocking(
                        runtime.handle_text,
                        session,
                        utterance.raw,
                        source="voice",
                        trace=trace,
                        cancel_event=cancel_event,
                        interaction_generation=interaction_generation,
                    )
                )
                listener = asyncio.ensure_future(websocket.receive())
                disconnected = False
                try:
                    while not routing.done():
                        finished, _pending = await asyncio.wait(
                            {routing, listener},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if listener not in finished:
                            continue
                        try:
                            frame = listener.result()
                        except WebSocketDisconnect:
                            disconnected = True
                            cancel_event.set()
                            break
                        if frame.get("type") == "websocket.disconnect":
                            # Do not schedule another receive on a closed
                            # socket; it would only fail and be discarded.
                            disconnected = True
                            cancel_event.set()
                            break
                        listener = asyncio.ensure_future(websocket.receive())
                        raw_frame = frame.get("text")
                        if not isinstance(raw_frame, str):
                            continue
                        try:
                            late = json.loads(raw_frame)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(late, dict) and late.get("type") == "cancel":
                            cancel_event.set()
                            trace.emit(
                                TraceStage.ROUTING,
                                "cancel_requested",
                                reason_code="client_cancelled",
                            )
                    # The routing thread cannot be killed, so it is always
                    # awaited; the token only stops work it had not started.
                    result = await routing
                finally:
                    listener.cancel()
                    await asyncio.gather(listener, return_exceptions=True)
                if cancel_event.is_set():
                    trace.emit(
                        TraceStage.ROUTING,
                        "cancel_observed",
                        reason_code="client_cancelled",
                    )
                if disconnected:
                    # A browser that has already gone away is not an error, and
                    # must not retire a healthy recognizer from the pool.
                    trace.emit(
                        TraceStage.AUDIO,
                        "disconnected",
                        reason_code="browser_disconnect",
                    )
                    break
                await websocket.send_json({"type": "assistant", **result.as_dict()})
                break
        except asyncio.TimeoutError:
            if trace is not None:
                trace.emit(
                    TraceStage.ERROR, "timeout", reason_code="audio_idle_timeout"
                )
            await _safe_ws_error(
                websocket, "audio_idle_timeout", "Voice session timed out."
            )
        except (
            SecurityError,
            SessionError,
            BrowserAudioError,
            STTEnginePoolError,
            ValueError,
        ) as exc:
            code = getattr(exc, "code", "invalid_request")
            if code == "stt_error":
                engine_reusable = False
            if trace is not None:
                trace.emit(TraceStage.ERROR, "failed", reason_code=code)
            await _safe_ws_error(websocket, code, str(exc))
        except WebSocketDisconnect:
            if trace is not None:
                trace.emit(
                    TraceStage.AUDIO, "disconnected", reason_code="browser_disconnect"
                )
        except Exception:
            engine_reusable = False
            LOGGER.exception("voice WebSocket failed")
            if trace is not None:
                trace.emit(TraceStage.ERROR, "failed", reason_code="internal_error")
            await _safe_ws_error(
                websocket, "internal_error", "Voice processing failed safely."
            )
        finally:
            # Every release below must happen even when teardown, cancellation,
            # or the socket close itself fails; otherwise a single recognizer
            # error would retire a voice slot for the life of the process.
            try:
                if stream is not None:
                    await run_teardown(stream.close, False)
                if engine_lease is not None:
                    await run_teardown(
                        stt_pool.release,
                        engine_lease.engine,
                        engine_reusable,
                    )
            finally:
                if acquired:
                    voice_slots.release()
                if capacity_acquired:
                    voice_capacity.release()
                try:
                    await websocket.close()
                except (RuntimeError, WebSocketDisconnect, OSError):
                    pass

    async def trace_socket(websocket: WebSocket) -> None:
        try:
            auth.require_origin(websocket.headers, websocket.headers.get("host"))
            client = websocket.client.host if websocket.client else None
            auth.admin_identity(websocket.headers, client)
            await websocket.accept()
            while True:
                await websocket.send_json(
                    {"type": "traces", "traces": runtime.traces.recent(20)}
                )
                await asyncio.sleep(0.75)
        except (SecurityError, WebSocketDisconnect):
            try:
                await websocket.close(code=1008)
            except RuntimeError:
                pass

    routes = [
        Route("/", index),
        Route("/admin", admin_page),
        Route("/healthz", health),
        Route("/readyz", ready),
        Route("/api/session", session_endpoint),
        Route("/api/session/conversation", clear_session, methods=["DELETE"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/planner", conversational_plan, methods=["POST"]),
        Route("/api/speech", speech, methods=["POST"]),
        Route("/api/auth/status", auth_status),
        Route("/api/auth/authenticate/options", auth_options, methods=["POST"]),
        Route("/api/auth/authenticate/verify", auth_verify, methods=["POST"]),
        Route("/api/auth/lock", auth_lock, methods=["POST"]),
        Route("/api/auth/passkeys", passkeys),
        Route(
            "/api/auth/passkeys/register/options",
            passkey_registration_options,
            methods=["POST"],
        ),
        Route(
            "/api/auth/passkeys/register/verify",
            passkey_registration_verify,
            methods=["POST"],
        ),
        Route("/api/auth/passkeys/label", passkey_label, methods=["POST"]),
        Route("/api/auth/passkeys/revoke", passkey_revoke, methods=["POST"]),
        Route(
            "/api/actions/pending/{pending_action_id}/cancel",
            cancel_pending_action,
            methods=["POST"],
        ),
        Route("/api/actions/jobs/{job_id}", action_job),
        Route("/api/actions/jobs/{job_id}/cancel", cancel_action_job, methods=["POST"]),
        Route("/api/capabilities", capabilities),
        Route("/api/admin/overview", overview),
        Route("/api/admin/traces", traces),
        Route("/api/admin/sessions", sessions),
        Route("/api/admin/routing/test", routing_test, methods=["POST"]),
        Route("/api/admin/models", model_status),
        Route("/api/admin/stt/test", stt_test, methods=["POST"]),
        Route("/api/admin/skills", skills),
        Route("/api/admin/skills/toggle", skill_toggle, methods=["POST"]),
        Route("/api/admin/skills/test", skill_test, methods=["POST"]),
        Route("/api/admin/tools", tools),
        Route("/api/admin/tools/desktop", desktop_tool_status),
        Route(
            "/api/admin/tools/desktop/ssh-test",
            desktop_tool_ssh_test,
            methods=["POST"],
        ),
        Route("/api/admin/tools/desktop/wake", desktop_tool_wake, methods=["POST"]),
        Route(
            "/api/admin/tools/desktop/shutdown",
            desktop_tool_shutdown,
            methods=["POST"],
        ),
        Route(
            "/api/admin/tools/desktop/streaming",
            desktop_tool_streaming,
            methods=["POST"],
        ),
        Route(
            "/api/admin/tools/desktop/launch-app",
            desktop_tool_launch,
            methods=["POST"],
        ),
        Route("/api/admin/tools/nas", nas_tool_status),
        Route("/api/admin/tools/wake-nas", wake_nas_tool, methods=["POST"]),
        Route("/api/admin/tools/shutdown-nas", shutdown_nas_tool, methods=["POST"]),
        Route("/api/admin/portal/identities", portal_identities),
        Route("/api/admin/portal/invite", portal_invite, methods=["POST"]),
        Route("/api/admin/portal/revoke", portal_revoke, methods=["POST"]),
        Route("/portal", portal_page),
        Route("/api/portal/status", portal_status),
        Route(
            "/api/portal/authenticate/options", portal_auth_options, methods=["POST"]
        ),
        Route("/api/portal/authenticate/verify", portal_auth_verify, methods=["POST"]),
        Route("/api/portal/register/options", portal_register_options, methods=["POST"]),
        Route("/api/portal/register/verify", portal_register_verify, methods=["POST"]),
        Route("/api/portal/sign-out", portal_sign_out, methods=["POST"]),
        Route("/api/portal/nas", portal_nas_status),
        # Wake is POST-only by construction: there is no GET route for it.
        Route("/api/portal/wake", portal_wake, methods=["POST"]),
        Route("/api/portal/shutdown/plan", portal_shutdown_plan, methods=["POST"]),
        Route(
            "/api/portal/shutdown/authenticate/options",
            portal_shutdown_auth_options,
            methods=["POST"],
        ),
        Route(
            "/api/portal/shutdown/authenticate/verify",
            portal_shutdown_auth_verify,
            methods=["POST"],
        ),
        Route("/api/portal/destination", portal_destination),
        Route("/api/admin/usage", usage),
        Route("/api/admin/security", security_status),
        Route("/api/admin/actions", admin_actions),
        Route("/api/admin/voice/presets", voice_presets),
        Route("/api/admin/voice/presets", save_voice_preset, methods=["POST"]),
        Route("/api/admin/voice/preview", preview_voice, methods=["POST"]),
        Route("/api/admin/ai/catalog", ai_catalog),
        Route("/api/admin/ai/settings", ai_settings),
        Route("/api/admin/ai/chat", ai_chat_settings, methods=["POST"]),
        Route("/api/admin/ai/tts", ai_tts_settings, methods=["POST"]),
        Route("/api/admin/integrations/openai", openai_credential),
        Route(
            "/api/admin/integrations/openai/test",
            openai_credential_test,
            methods=["POST"],
        ),
        Route(
            "/api/admin/integrations/openai/key",
            openai_credential_set,
            methods=["POST"],
        ),
        Route(
            "/api/admin/integrations/openai/key",
            openai_credential_remove,
            methods=["DELETE"],
        ),
        Route("/api/admin/system", system_info),
        Route("/api/admin/logs", logs),
        Route("/api/admin/codex/jobs", codex_jobs),
        Route("/api/admin/codex/jobs", create_skill_job, methods=["POST"]),
        Route("/api/admin/codex/jobs/{job_id}", codex_job_detail),
        Route("/api/admin/codex/jobs/{job_id}/run", run_skill_job, methods=["POST"]),
        Route(
            "/api/admin/codex/jobs/{job_id}/decision",
            decide_skill_job,
            methods=["POST"],
        ),
        WebSocketRoute("/ws/voice", voice_socket),
        WebSocketRoute("/ws/admin/traces", trace_socket),
        Route("/api/admin/integrations/openai-usage", usage_admin_state),
        Route(
            "/api/admin/integrations/openai-usage/sync",
            usage_admin_sync,
            methods=["POST"],
        ),
        Route(
            "/api/admin/integrations/openai-usage/test",
            usage_admin_test,
            methods=["POST"],
        ),
        Route(
            "/api/admin/integrations/openai-usage/key",
            usage_admin_key_set,
            methods=["POST"],
        ),
        Route(
            "/api/admin/integrations/openai-usage/key",
            usage_admin_key_remove,
            methods=["DELETE"],
        ),
        Route(
            "/api/admin/integrations/openai-usage/project",
            usage_admin_project,
            methods=["POST"],
        ),
        Route("/api/admin/appearance", appearance_settings),
        Route("/api/admin/appearance", apply_appearance, methods=["POST"]),
        # Registered ahead of the wildcard so the generated stylesheet is not
        # shadowed by the static allow-list lookup.
        Route("/assets/theme.css", theme_stylesheet),
        Route("/assets/{asset_name:str}", public_asset),
    ]
    # The route is absent under the committed default. It is reachable only
    # from the separately gated private TLS ingress when explicitly enabled.
    if configured.agent_ingress.enabled:
        routes.insert(
            -3, WebSocketRoute("/agent/v1/session", runtime.desktop_agent.socket)
        )
    if configured.nas_agent_ingress.enabled:
        routes.insert(
            -3, WebSocketRoute("/nas-agent/v1/session", runtime.nas_agent.socket)
        )

    async def shutdown_workers() -> None:
        worker_pool.shutdown(wait=True, cancel_futures=True)
        stt_pool.close()
        teardown_pool.shutdown(wait=True, cancel_futures=True)
        runtime.local_tts.close()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        try:
            try:
                lease = await run_blocking(stt_pool.warm)
                LOGGER.info(
                    "local STT ready (cold=%s initialization_ms=%.1f)",
                    not lease.reused,
                    lease.initialization_seconds * 1000,
                )
            except Exception:  # Text service remains available after prewarm failure.
                LOGGER.exception("local STT prewarm failed; voice will retry lazily")
            yield
        finally:
            await shutdown_workers()

    # Registered centrally so a future route cannot leak an uncaught security or
    # session failure into ServerErrorMiddleware as an opaque 500.
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={
            SecurityError: _handle_typed_exception,
            SessionError: _handle_typed_exception,
            SpeechProviderError: _handle_typed_exception,
            SkillAuthoringError: _handle_typed_exception,
            WebAuthnError: _handle_typed_exception,
            AuthStateError: _handle_typed_exception,
            ActionStateError: _handle_typed_exception,
            ActionCoordinatorError: _handle_typed_exception,
        },
    )
    app.state.service = runtime
    app.state.settings = configured
    app.state.worker_pool = worker_pool
    app.state.stt_pool = stt_pool
    app.state.shutdown_workers = shutdown_workers
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def _new_stt_engine():
    from butters.stt.sherpa_engine import SherpaOnnxStreamingSTT

    settings = load_stt_settings()
    return SherpaOnnxStreamingSTT(
        settings.model_dir,
        num_threads=settings.num_threads,
        decoding_method=settings.decoding_method,
        sherpa_endpoint_enabled=False,
        max_utterance_seconds=settings.max_utterance_seconds,
    )


def _state_directory_check(state_dir: Path) -> str:
    """Writability, not existence.

    Every ledger the service depends on is SQLite under this directory, so a
    directory that exists but cannot be written to is not readiness.
    """

    if not state_dir.is_dir():
        return "unavailable"
    probe = state_dir / ".readyz-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError:
        return "unavailable"
    return "ready"


def _configuration_check(runtime: BetaAssistantService) -> str:
    """The configuration is only ready if it produced a usable system."""

    try:
        entities = runtime.assistant.router.entities.entities
        skills = runtime.assistant.skills.skills
    except Exception:
        LOGGER.warning("readiness configuration check failed", exc_info=True)
        return "unavailable"
    return "ready" if entities and skills else "unavailable"


def _router_check(runtime: BetaAssistantService) -> str:
    """Drive the real router rather than asserting it exists.

    A fixed phrase is enough: the point is that normalization, the entity
    registry and the metric registry all resolve without raising. The intent it
    produces is deliberately not asserted, so this stays valid when the
    deployment's entity set changes.
    """

    try:
        routed = runtime.assistant.preview_route("what is the temperature")
    except Exception:
        LOGGER.warning("readiness router check failed", exc_info=True)
        return "unavailable"
    return "ready" if routed is not None else "unavailable"


def _broker_check(configured: AssistantSettings) -> str:
    """Presence only. Connecting would activate a root service.

    The socket is systemd-activated, so a connection from here would start the
    privileged broker as a side effect of a readiness probe. Reported, never
    gating: a beta install without the broker is a supported deployment.
    """

    if not configured.broker.enabled:
        return "disabled"
    try:
        return "ready" if Path(configured.broker.socket_path).exists() else "unavailable"
    except OSError:
        return "unavailable"


def _string_field(payload: dict[str, object], key: str) -> str:
    """Require one non-empty string field, reported as a 400 by the boundary."""

    value = payload.get(key)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"{key} must be a non-empty string")


def _ceremony_body(payload: dict[str, object]) -> tuple[str, dict[str, object]]:
    ceremony_id = _string_field(payload, "ceremony_id")
    credential = payload.get("credential")
    if isinstance(credential, dict):
        return ceremony_id, credential
    raise ValueError("credential is required")


def _audio_seconds_header(value: float | None, digits: int) -> str:
    """Report an unmeasurable duration as "unknown" rather than as a number.

    A streaming WAV carries placeholder chunk sizes; trusting them is what
    reported a 1.2 second clip as 89478 seconds. When the duration genuinely
    cannot be derived, saying so is the only honest option - 0 would read as
    silence that is not there.
    """

    return "unknown" if value is None else str(round(value, digits))


def _admin(request: Request, auth: AuthPolicy) -> str:
    client = request.client.host if request.client else None
    return auth.admin_identity(request.headers, client)


def _admin_mutation(
    request: Request, runtime: BetaAssistantService, auth: AuthPolicy
) -> str:
    identity = _admin(request, auth)
    _mutation_session(request, runtime, auth)
    return identity


def _session_from_request(
    request: Request,
    runtime: BetaAssistantService,
    *,
    required: bool = True,
) -> BrowserSession | None:
    value = request.cookies.get(SESSION_COOKIE)
    session = runtime.sessions.get(value)
    if required and session is None:
        raise SessionError("invalid_session", "browser session is invalid or expired")
    return session


def _mutation_session(
    request: Request, runtime: BetaAssistantService, auth: AuthPolicy
) -> BrowserSession:
    auth.require_origin(request.headers, request.headers.get("host"))
    session = _bound_session(request, runtime, auth)
    AuthPolicy.require_csrf(session, request.headers.get("x-butters-csrf"))
    return session


def _bound_session(
    request: Request,
    runtime: BetaAssistantService,
    auth: AuthPolicy,
) -> BrowserSession:
    session = _session_from_request(request, runtime)
    assert session is not None
    _require_session_peer(request, session, auth)
    return session


def _require_session_peer(
    request: Request,
    session: BrowserSession,
    auth: AuthPolicy,
) -> None:
    client = request.client.host if request.client else None
    _require_peer_identity(request.headers, client, session, auth)


def _require_peer_identity(
    headers: object,
    client_host: str | None,
    session: BrowserSession,
    auth: AuthPolicy,
) -> None:
    """Bind a browser session to the identity that created it.

    The HTTP surface and `/ws/voice` share this one check so the WebSocket
    cannot drift back into a weaker rule. `AuthPolicy.peer_key` already
    degrades a missing identity header or an untrusted proxy topology to a
    socket-peer key, so a session created under a tailnet identity matches
    neither, and the failure is reported with the same opaque code the HTTP
    path uses.
    """

    if session.peer_key != auth.peer_key(headers, client_host):
        raise SecurityError(
            "session_identity_denied",
            "browser session belongs to another identity",
        )


async def _json_body(request: Request, maximum: int) -> dict[str, object]:
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        raise ValueError("Content-Type must be application/json")
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > maximum:
                raise ValueError("request body is too large")
        except ValueError as exc:
            if str(exc) == "request body is too large":
                raise
            raise ValueError("Content-Length is invalid") from exc
    raw = await _bounded_body(request, maximum)
    if len(raw) > maximum:
        raise ValueError("request body is too large")
    try:
        # Python accepts the non-standard NaN/Infinity literals by default; they
        # would otherwise flow into numeric parameters as unusable floats.
        value = json.loads(raw, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


def _reject_constant(name: str) -> float:
    raise ValueError(f"request JSON must not contain {name}")


def _bounded_client_ms(value: object) -> int | None:
    """Retain optional untrusted browser timing only inside a harmless bound."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value) if 0 <= value <= 300_000 else None


def _interaction_generation(value: object) -> int | None:
    """Parse the optional shipped-browser ordering token within JS precision."""

    if value is None:
        return None
    if isinstance(value, str):
        if not value.isdigit():
            raise ValueError("interaction generation is invalid")
        value = int(value)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 9_007_199_254_740_991
    ):
        raise ValueError("interaction generation is invalid")
    return value


async def _bounded_body(request: Request, maximum: int) -> bytes:
    """Read a possibly chunked request without ever buffering past the limit."""

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise ValueError("request body is too large")
        body.extend(chunk)
    return bytes(body)


async def _acquire(semaphore: asyncio.Semaphore) -> bool:
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.05)
        return True
    except asyncio.TimeoutError:
        return False


async def _send_audio_events(
    websocket: WebSocket, events: tuple[Any, ...], trace: Any
) -> None:
    for event in events:
        if event.kind == "speech_start":
            trace.emit(TraceStage.STT, "speech_start", fields={"provider": "local"})
        elif event.kind == "partial":
            trace.emit(
                TraceStage.STT,
                "partial",
                fields={"partial": event.text, "provider": "local"},
            )
        await websocket.send_json({"type": event.kind, "text": event.text})


async def _safe_ws_error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_json(
            {
                "type": "error",
                "code": code,
                "message": sanitize_text(message, max_bytes=512).text,
            }
        )
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _handle_typed_exception(_request: Request, exc: Exception) -> JSONResponse:
    return _exception_response(exc)


def _exception_response(
    exc: Exception, *, safe_to_retry: bool = False
) -> JSONResponse:
    if isinstance(exc, SecurityError):
        return _error(exc.code, str(exc), exc.status_code)
    if isinstance(exc, SessionError):
        return _error(
            exc.code,
            str(exc),
            exc.status_code,
            safe_to_retry=safe_to_retry and exc.code == "invalid_session",
        )
    if isinstance(exc, PortalError):
        return _error(exc.code, str(exc), exc.status_code)
    if isinstance(exc, SpeechProviderError):
        return _error(exc.code, str(exc), 503 if "unavailable" in exc.code else 400)
    if isinstance(exc, CapabilityError):
        # A rejected provider/model/parameter combination is a client error and
        # keeps its specific code, so the browser can point at the right field.
        return _error(exc.code, str(exc), 400)
    if isinstance(exc, CredentialError):
        return _error(
            exc.code, str(exc), 409 if exc.code == "credential_missing" else 400
        )
    if isinstance(exc, SkillAuthoringError):
        status = (
            404
            if exc.code == "job_not_found"
            else 409
            if exc.code
            in {
                "dirty_worktree",
                "base_commit_changed",
                "invalid_job_state",
                "job_already_running",
            }
            else 503
            if exc.code == "repository_unavailable"
            else 400
        )
        return _error(exc.code, str(exc), status)
    if isinstance(
        exc,
        (WebAuthnError, AuthStateError, ActionStateError, ActionCoordinatorError),
    ):
        code = exc.code
        status = (
            404
            if code in {"job_denied", "pending_action_denied", "credential_denied"}
            else 409
            if "replay" in code or "expired" in code or "unavailable" in code
            else 403
            if "denied" in code or "required" in code
            else 400
        )
        return _error(code, str(exc), status)
    if isinstance(exc, PermissionError):
        return _error("forbidden", "request is not authorized", 403)
    return _error("invalid_request", str(exc), 400)


def _error(
    code: str,
    message: str,
    status: int,
    *,
    safe_to_retry: bool = False,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": code[:64],
            "message": sanitize_text(message, max_bytes=512).text,
            "safe_to_retry": safe_to_retry,
        },
        status_code=status,
    )


def _bounded_query_int(
    request: Request, name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is outside the allowed range")
    return value


def _jsonable(value: object) -> object:
    if value is None:
        return None
    candidate = asdict(value) if hasattr(value, "__dataclass_fields__") else value  # type: ignore[arg-type]
    clean, _redactions = sanitize_value(candidate, max_text_bytes=2048)
    encoded = json.dumps(clean, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        return {
            "summary": sanitize_text(encoded, max_bytes=16 * 1024).text,
            "truncated": True,
        }
    return clean
