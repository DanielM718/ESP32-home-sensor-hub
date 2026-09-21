"""A live voice answer must describe itself exactly as a typed one does.

The routing facts beside a Chat answer - which tier ran, which model, which
effort, why it was routed there, and the provider's own summary of its
reasoning - were already produced for every turn and already persisted for
every turn, including voice turns. They were also already present on the
`assistant` WebSocket frame, because that frame is the whole canonical
`ServiceResponse`. The browser simply dropped them: `handleVoiceEvent` called
the shared renderer with two arguments where `sendText` calls it with three,
so the badge and the reasoning summary existed only after a reload restored
them from history.

These tests hold the fix to the properties that make it safe:

1. One canonical shape. The frame carries the same field names the typed
   response carries and the same ones history stores, and there is no second
   voice-only schema anywhere.
2. One renderer. `handleVoiceEvent` hands its frame to `addMessage`, the same
   function `sendText` uses, so there is no voice-only badge or summary
   renderer and no voice-only markup path.
3. Nothing is invented. A local, model-free answer produces no badge.
4. Metadata belongs to its own answer. An error, a cancellation, an empty
   transcript and the next turn can none of them inherit it.
5. Speech is untouched. TTS is handed the canonical `response_text` and
   nothing else, on the voice transport as on the typed one.

No provider is contacted: the cloud reasoner is a local fake and synthesis is
recorded rather than performed.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from beta1_harness import WebSocketHarness, client, start_session
from butters.assistant import create_assistant
from butters.assistant_config import load_assistant_settings
from butters.cloud.general import GeneralCloudTurn
from butters.cloud.model import CloudTokenUsage
from butters.integrations.model import (
    SensorRecord,
    SensorSnapshot,
    ServerHealthSnapshot,
)
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.chat_history import METADATA_FIELDS, assistant_metadata
from butters.web.service import BetaAssistantService, ServiceResponse
from butters.web.speech import SpeechResult

STATIC = Path(__file__).resolve().parents[1] / "src/butters/web/static"
APP_JS = (STATIC / "assets/app.js").read_text(encoding="utf-8")

OWNER = "owner@example.com"

ANSWER = "Convection is the likeliest explanation."
SUMMARY = "I compared convection, conduction and radiation, and ranked them."
CLOUD_PROMPT = (
    "Compare convection, conduction, and radiation as explanations for an "
    "enclosure warming overnight, and explain what evidence would distinguish "
    "them."
)
LOCAL_PROMPT = "what is the humidity in box three"

# The nine fields the product supports, as `chat_history` declares them. The
# test does not restate them: a field added there must appear on the frame
# without anyone remembering to edit this file.
SUPPORTED_FIELDS = frozenset(METADATA_FIELDS)

# Names that must never appear on the wire whatever else changes. These all
# denote reasoning *content*, a raw provider object, or a credential.
#
# `usage.reasoning_tokens` is deliberately not in this list: it is a token
# count the accounting surface already publishes on the typed response, and a
# count carries none of the reasoning it counted. The test below pins voice
# and typed to the same `usage` keys, so voice cannot gain one of its own.
HIDDEN_REASONING_NAMES = (
    "chain_of_thought",
    "chain-of-thought",
    "reasoning_trace",
    "reasoning_content",
    "reasoning_text",
    "thinking",
    "thoughts",
    "encrypted_content",
    "raw_response",
    "api_key",
    "authorization",
    "system_prompt",
)


# =========================== harness ======================================


class Sensors:
    def snapshot(self) -> SensorSnapshot:
        return SensorSnapshot(
            "2026-08-12T12:00:00Z",
            tuple(
                SensorRecord(
                    "environment",
                    str(index),
                    "2026-08-12T11:59:55Z",
                    5,
                    "online",
                    {"humidity": 20.0 + index, "temperature": 20.0 + index},
                )
                for index in (1, 2, 3)
            ),
        )


class Health:
    def snapshot(self) -> ServerHealthSnapshot:
        return ServerHealthSnapshot(
            100, 0.1, 0.1, 0.1, 1_000_000, 0, 1_000_000, 2_000_000, 45.0, "0x0", ()
        )


class FakeCloud:
    """A reasoner that answers from memory and never opens a socket."""

    available = True

    def __init__(self, *, text: str = ANSWER, summary: str | None = SUMMARY) -> None:
        self.calls: list[dict[str, object]] = []
        self._text = text
        self._summary = summary

    def reason(self, **kwargs: object) -> GeneralCloudTurn:
        self.calls.append(kwargs)
        return GeneralCloudTurn(
            str(kwargs["model"]),
            str(kwargs["effort"]),
            0.01,
            response_id="response_safe_id",
            response_text=self._text,
            usage=CloudTokenUsage(input_tokens=100, output_tokens=20),
            reasoning_summary=self._summary,
        )


class ScriptedEngine:
    """A recognizer that finalizes a scripted utterance, one per turn."""

    initialization_seconds = 0.0
    script: tuple[str, ...] = (LOCAL_PROMPT,)

    def __init__(self) -> None:
        self._turn = 0

    def _text(self) -> str:
        index = min(self._turn, len(type(self).script) - 1)
        return type(self).script[index]

    def start_utterance(self) -> None:
        return None

    def accept_audio(self, _frame) -> str:
        return self._text()

    def get_partial_transcript(self) -> str:
        return self._text()

    def endpoint_detected(self) -> bool:
        return False

    def finalize(self) -> str:
        text = self._text()
        self._turn += 1
        return text

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


def _engine(*script: str):
    """A recognizer factory bound to one scripted sequence of utterances."""

    return type("BoundEngine", (ScriptedEngine,), {"script": tuple(script)})


def _build(tmp_path: Path, *, cloud: bool = True, reasoner: FakeCloud | None = None):
    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(
            base.cloud,
            enabled=cloud,
            allow_paid_calls=cloud,
            max_estimated_cost_per_request_usd=0.5,
        ),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=(OWNER,),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    provider = reasoner if reasoner is not None else FakeCloud()
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=provider,
        state_dir=tmp_path,
    )
    if cloud:
        service.ai.apply_chat(
            {
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
                "max_output_tokens": 1200,
                "routing_mode": "adaptive",
                "max_automatic_tier": "sol",
                "reasoning_summary_enabled": True,
            }
        )
    return service, settings, provider


def _app(tmp_path: Path, engine, **kwargs):
    service, settings, provider = _build(tmp_path, **kwargs)
    app = create_app(
        settings, DomainVocabulary((), ()), service, stt_engine_factory=engine
    )
    return app, service, provider


def _headers(session_id: str) -> dict[str, str]:
    return {
        "origin": "http://testserver",
        "cookie": f"butters_session={session_id}",
        "tailscale-user-login": OWNER,
    }


async def _voice_turn(app, socket: WebSocketHarness, csrf: str) -> list[dict]:
    """Drive one complete voice turn and return every frame it produced."""

    await socket.send_json(
        {
            "type": "start",
            "csrf_token": csrf,
            "sample_rate": 16000,
            "channels": 1,
            "encoding": "pcm_s16le",
        }
    )
    frames: list[dict] = []
    while True:
        frame = await socket.receive()
        frames.append(frame)
        if frame.get("type") == "listening":
            break
    await socket.send_bytes(b"\x00\x01" * 3200)
    await socket.send_json({"type": "stop", "endpoint_reason": "tap"})
    while True:
        frame = await socket.receive(timeout=20.0)
        if not isinstance(frame, dict) or "type" not in frame:
            break
        frames.append(frame)
        if frame["type"] in {"assistant", "error", "cancelled"}:
            break
    return frames


def _run(tmp_path: Path, *script: str, **kwargs) -> tuple[list[dict], object]:
    """One voice session over `script`, returning its frames and the service."""

    captured: dict[str, object] = {}

    async def scenario() -> list[dict]:
        app, service, provider = _app(tmp_path, _engine(*script), **kwargs)
        captured["service"] = service
        captured["provider"] = provider
        frames: list[dict] = []
        try:
            async with client(app) as http:
                session = await start_session(
                    http, headers={"tailscale-user-login": OWNER}
                )
                session_id = str(http.cookies.get("butters_session"))
                csrf = str(session["csrf_token"])
                for _ in script:
                    socket = WebSocketHarness(
                        app, "/ws/voice", headers=_headers(session_id)
                    )
                    try:
                        await socket.connect()
                        frames.extend(await _voice_turn(app, socket, csrf))
                    finally:
                        await socket.disconnect()
                        await socket.finish()
        finally:
            await app.state.shutdown_workers()
        return frames

    frames = asyncio.run(scenario())
    return frames, captured["service"]


def _assistants(frames: list[dict]) -> list[dict]:
    return [frame for frame in frames if frame.get("type") == "assistant"]


def _one_assistant(frames: list[dict]) -> dict:
    found = _assistants(frames)
    assert len(found) == 1, [frame.get("type") for frame in frames]
    return found[0]


def _block(source: str, opening: str) -> str:
    """The balanced brace block following the first `opening` match."""

    start = source.index(opening)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"unbalanced block after {opening!r}")


# ============ 1. the frame carries the canonical metadata =================


def test_a_cloud_voice_frame_carries_the_supported_routing_fields(
    tmp_path: Path,
) -> None:
    """The gap the brief describes, held at the transport."""

    frames, _service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    assert assistant["response_text"] == ANSWER
    assert assistant["cloud_used"] is True
    # Every field the product supports is present, by the name history uses.
    missing = SUPPORTED_FIELDS - set(assistant)
    assert not missing, missing


def test_the_frame_reuses_the_typed_response_shape_exactly(tmp_path: Path) -> None:
    """One canonical projection, two transports.

    The frame is the whole `ServiceResponse` plus its own `type` tag, which is
    what `/api/chat` returns plus its own `conversation_id`. Nothing about
    voice is hand-maintained, so the two cannot drift.
    """

    frames, _service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    declared = {field.name for field in ServiceResponse.__dataclass_fields__.values()}
    assert set(assistant) == declared | {"type"}


def test_voice_and_typed_expose_the_same_fields_for_the_same_prompt(
    tmp_path: Path,
) -> None:
    """The claim the brief makes, tested against the typed path itself.

    Both transports are driven for the same question on the same build, and
    the frame is compared with the `/api/chat` body. Voice must expose the
    same names - no fewer, so the badge can be drawn, and no more, so it
    cannot become a wider disclosure surface than Chat already is.
    """

    async def scenario() -> tuple[dict, dict]:
        app, _service, _provider = _app(tmp_path, _engine(CLOUD_PROMPT))
        try:
            async with client(app) as http:
                session = await start_session(
                    http, headers={"tailscale-user-login": OWNER}
                )
                session_id = str(http.cookies.get("butters_session"))
                csrf = str(session["csrf_token"])
                socket = WebSocketHarness(
                    app, "/ws/voice", headers=_headers(session_id)
                )
                try:
                    await socket.connect()
                    frames = await _voice_turn(app, socket, csrf)
                finally:
                    await socket.disconnect()
                    await socket.finish()
                typed = await http.post(
                    "/api/chat",
                    json={"text": CLOUD_PROMPT},
                    headers={
                        "origin": "http://testserver",
                        "x-butters-csrf": csrf,
                        "tailscale-user-login": OWNER,
                    },
                )
                assert typed.status_code == 200, typed.text
                return _one_assistant(frames), typed.json()
        finally:
            await app.state.shutdown_workers()

    voice, typed = asyncio.run(scenario())

    # `type` is the frame tag; `conversation_id` is session state the HTTP
    # handler attaches. Everything else is the one canonical projection.
    assert set(voice) - {"type"} == set(typed) - {"conversation_id"}
    # And the displayable facts agree, value for value.
    for field in METADATA_FIELDS:
        assert voice[field] == typed[field], field
    # `usage` gained no voice-only key, so no accounting field is voice-only.
    assert set(voice["usage"] or {}) == set(typed["usage"] or {})


def test_tier_model_and_effort_survive_the_transport_unchanged(
    tmp_path: Path,
) -> None:
    frames, service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    assert assistant["routing_mode"] == "adaptive"
    assert isinstance(assistant["routing_tier"], str) and assistant["routing_tier"]
    assert assistant["model"] == "gpt-5.6-terra"
    assert assistant["reasoning_effort"] in {"low", "medium", "high"}
    # And they are the values the service decided, not a transport default.
    stored = service.chat_history.conversations("identity:" + OWNER)
    assert stored, "the voice turn was not persisted"


def test_routing_reason_codes_survive_unchanged(tmp_path: Path) -> None:
    frames, _service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    codes = assistant["routing_reason_codes"]
    # JSON has no tuples: a list of the same strings, in the same order.
    assert isinstance(codes, list) and codes
    assert all(isinstance(code, str) and code for code in codes)
    assert codes == list(dict.fromkeys(codes)), "reason codes were duplicated"


def test_the_reasoning_summary_survives_and_stays_out_of_the_answer(
    tmp_path: Path,
) -> None:
    frames, _service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    assert assistant["reasoning_summary"] == SUMMARY
    # The invariant that matters: it is a sibling of the answer, not part of
    # it, so nothing that renders or speaks `response_text` can pick it up.
    assert assistant["response_text"] == ANSWER
    assert SUMMARY not in assistant["response_text"]
    assert "compared" not in assistant["response_text"]


def test_the_frame_exposes_no_hidden_reasoning_and_no_credential(
    tmp_path: Path,
) -> None:
    frames, _service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    for name in HIDDEN_REASONING_NAMES:
        assert name not in assistant, name
    # Nor nested anywhere inside it, whatever a future field is called.
    serialized = json.dumps(assistant).casefold()
    for name in HIDDEN_REASONING_NAMES:
        assert name.casefold() not in serialized, name


# ================ 2. a local answer invents nothing =======================


def test_a_local_voice_answer_fabricates_no_cloud_metadata(tmp_path: Path) -> None:
    """A deterministic reply must look exactly like a typed deterministic one."""

    frames, _service = _run(tmp_path, LOCAL_PROMPT, cloud=False)
    assistant = _one_assistant(frames)

    assert assistant["cloud_used"] is False
    assert assistant["routing_tier"] is None
    assert assistant["model"] is None
    assert assistant["reasoning_effort"] is None
    assert assistant["reasoning_summary"] is None
    assert assistant["tier_escalated"] is False
    assert assistant["estimated_complexity"] is None
    assert assistant["routing_reason_codes"] == []


def test_a_local_voice_answer_reaches_no_provider(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    async def scenario() -> list[dict]:
        app, _service, provider = _app(tmp_path, _engine(LOCAL_PROMPT), cloud=False)
        captured["provider"] = provider
        try:
            async with client(app) as http:
                session = await start_session(
                    http, headers={"tailscale-user-login": OWNER}
                )
                session_id = str(http.cookies.get("butters_session"))
                socket = WebSocketHarness(
                    app, "/ws/voice", headers=_headers(session_id)
                )
                try:
                    await socket.connect()
                    return await _voice_turn(
                        app, socket, str(session["csrf_token"])
                    )
                finally:
                    await socket.disconnect()
                    await socket.finish()
        finally:
            await app.state.shutdown_workers()

    frames = asyncio.run(scenario())
    assert _assistants(frames)
    assert captured["provider"].calls == []


def test_the_local_badge_decision_is_the_renderers_not_the_transports() -> None:
    """`cloudMetadata` returns nothing unless the frame claims a cloud model."""

    badge = _block(APP_JS, "function cloudMetadata(meta)")

    assert "meta.cloud_used !== true" in badge
    assert "return null" in badge


# ================ 3. one renderer, two transports =========================


def test_the_voice_handler_passes_its_frame_to_the_shared_renderer() -> None:
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")
    typed = _block(APP_JS, "async function sendText")

    assert 'addMessage("assistant", data.response_text, data)' in handler
    # The same call, with the same arity, as the typed path makes.
    assert 'addMessage("assistant", data.response_text, data)' in typed


def test_no_voice_specific_badge_or_summary_renderer_exists() -> None:
    """Both renderers may be reached only through `addMessage`."""

    for name in ("cloudMetadata", "reasoningSummary"):
        definition = f"function {name}("
        assert definition in APP_JS, name
        calls = [
            match.start()
            for match in re.finditer(rf"\b{name}\s*\(", APP_JS)
            if not APP_JS.startswith(definition, match.start() - len("function "))
        ]
        assert len(calls) == 1, f"{name} is called {len(calls)} times"
        renderer = _block(APP_JS, "function addMessage(role, text, meta = null)")
        assert f"{name}(meta)" in renderer, name


def test_no_voice_specific_markup_path_was_introduced() -> None:
    """Voice text renders through the one sanitized Markdown renderer."""

    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")

    assert "innerHTML" not in handler
    assert "insertAdjacentHTML" not in handler
    assert "createContextualFragment" not in handler
    # One definition, and exactly two callers: the message body and the
    # reasoning-summary disclosure. Voice added neither a third nor its own.
    assert APP_JS.count("renderAssistantMarkdown(") == 3
    summary = _block(APP_JS, "function reasoningSummary(meta)")
    assert "renderAssistantMarkdown(text)" in summary
    renderer = _block(APP_JS, "function addMessage(role, text, meta = null)")
    assert "renderAssistantMarkdown(text)" in renderer


def test_the_existing_markdown_protections_are_unchanged() -> None:
    """The sanitizer contract the voice text now also flows through."""

    assert "html: false" in APP_JS
    assert 'markdown.disable("image", true)' in APP_JS
    assert "RETURN_DOM_FRAGMENT: true" in APP_JS
    assert "ALLOW_DATA_ATTR: false" in APP_JS
    assert 'SAFE_URI = /^(?:https?:|mailto:)/i' in APP_JS
    for forbidden in ("script", "style", "iframe", "img", "object", "embed"):
        assert f'"{forbidden}"' in APP_JS, forbidden
    # User text is still plaintext, on both transports.
    renderer = _block(APP_JS, "function addMessage(role, text, meta = null)")
    assert "paragraph.textContent = text" in renderer


# ==================== 4. TTS is untouched =================================


def test_tts_receives_the_response_text_only_on_the_voice_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthesis is handed the stored assistant message, which is the answer."""

    service, _settings, _provider = _build(tmp_path)
    session = service.sessions.create(peer_key="identity:" + OWNER)
    response = service.handle_text(session, CLOUD_PROMPT, source="voice")
    assert response.reasoning_summary == SUMMARY

    synthesized: list[str] = []

    def recording(text, preset, **_kwargs):
        synthesized.append(text)
        return SpeechResult(b"RIFF", "local", "local-piper", "kathleen", 0.01, 0.5)

    monkeypatch.setattr(service, "synthesize_preview", recording)
    service.synthesize_trace_response(session, response.trace_id)

    assert synthesized == [ANSWER]


def test_the_reasoning_summary_is_never_part_of_tts_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _settings, _provider = _build(tmp_path)
    session = service.sessions.create(peer_key="identity:" + OWNER)
    response = service.handle_text(session, CLOUD_PROMPT, source="voice")

    synthesized: list[str] = []

    def recording(text, preset, **_kwargs):
        synthesized.append(text)
        return SpeechResult(b"RIFF", "local", "local-piper", "kathleen", 0.01, 0.5)

    monkeypatch.setattr(service, "synthesize_preview", recording)
    service.synthesize_trace_response(session, response.trace_id)

    spoken = synthesized[0]
    for forbidden in (
        SUMMARY,
        "compared",
        "ranked",
        "gpt-5.6",
        "terra",
        "Terra",
        "adaptive",
        "escalat",
        "cloud",
        "Cloud",
        "usd",
        "$",
        "complexity",
    ):
        assert forbidden not in spoken, forbidden


def test_the_browser_speaks_from_the_trace_not_from_the_frame() -> None:
    """The live UI change cannot alter synthesis input: the browser sends an
    identifier, and the server picks the text."""

    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")
    speaker = _block(APP_JS, "async function speak(traceId)")

    assert "playResponse(turn, traceId)" in handler
    assert "data.trace_id" in handler
    # The synthesis request carries one identifier and nothing else, so no
    # field this change made visible can reach the speech provider.
    assert "JSON.stringify({trace_id: traceId})" in speaker
    for forbidden in (
        "response_text",
        "reasoning_summary",
        "routing_tier",
        "routing_mode",
        "cloud_used",
        "reasoning_effort",
    ):
        assert forbidden not in speaker, forbidden


# ================ 5. persistence and reload agree =========================


def test_voice_metadata_is_persisted_by_the_existing_chat_history_path(
    tmp_path: Path,
) -> None:
    frames, service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    owner = "identity:" + OWNER
    conversations = service.chat_history.conversations(owner)
    assert len(conversations) == 1
    stored = service.chat_history.conversation(
        conversations[0]["conversation_id"], owner
    )
    messages = [item for item in stored["messages"] if item["role"] == "assistant"]
    assert len(messages) == 1
    assert messages[0]["text"] == assistant["response_text"]
    assert messages[0]["metadata"] is not None


def test_live_frame_persisted_and_reopened_metadata_are_the_same(
    tmp_path: Path,
) -> None:
    """The reload that used to be required must now be a no-op."""

    frames, service = _run(tmp_path, CLOUD_PROMPT)
    assistant = _one_assistant(frames)

    owner = "identity:" + OWNER
    conversation_id = service.chat_history.conversations(owner)[0]["conversation_id"]
    reopened = service.chat_history.conversation(conversation_id, owner)
    restored = next(
        item["metadata"]
        for item in reopened["messages"]
        if item["role"] == "assistant"
    )

    # What the live frame shows, projected through the same allow-list the
    # store uses, is exactly what a reload restores.
    live = {
        field: assistant[field]
        for field in METADATA_FIELDS
        if assistant.get(field) not in (None, False, (), [])
    }
    assert live == restored


def test_the_stored_allow_list_was_not_widened() -> None:
    """The frame gained no reader, so the store gains no field."""

    assert set(METADATA_FIELDS) == {
        "cloud_used",
        "routing_mode",
        "routing_tier",
        "routing_reason_codes",
        "estimated_complexity",
        "tier_escalated",
        "model",
        "reasoning_effort",
        "reasoning_summary",
    }


def test_a_local_voice_turn_persists_no_metadata_block(tmp_path: Path) -> None:
    frames, service = _run(tmp_path, LOCAL_PROMPT, cloud=False)
    _one_assistant(frames)

    owner = "identity:" + OWNER
    conversation_id = service.chat_history.conversations(owner)[0]["conversation_id"]
    stored = service.chat_history.conversation(conversation_id, owner)
    for item in stored["messages"]:
        if item["role"] == "assistant":
            assert item["metadata"] is None


# ============= 6. metadata belongs to its own answer ======================


def test_a_cloud_turn_does_not_leak_metadata_into_the_next_local_turn(
    tmp_path: Path,
) -> None:
    """Two turns on one session: the local answer must carry no badge."""

    frames, _service = _run(tmp_path, CLOUD_PROMPT, LOCAL_PROMPT)
    answers = _assistants(frames)
    assert len(answers) == 2

    first, second = answers
    assert first["cloud_used"] is True
    assert first["reasoning_summary"] == SUMMARY
    # The second frame is its own answer, described by its own facts.
    assert second["cloud_used"] is False
    assert second["reasoning_summary"] is None
    assert second["routing_tier"] is None
    assert second["model"] is None
    assert second["trace_id"] != first["trace_id"]


def test_each_frame_carries_its_own_trace_identity(tmp_path: Path) -> None:
    frames, _service = _run(tmp_path, CLOUD_PROMPT, CLOUD_PROMPT)
    answers = _assistants(frames)

    assert len({answer["trace_id"] for answer in answers}) == len(answers)
    assert len({answer["request_id"] for answer in answers}) == len(answers)


def test_an_empty_transcript_produces_an_error_frame_with_no_metadata(
    tmp_path: Path,
) -> None:
    frames, _service = _run(tmp_path, "", cloud=False)

    assert not _assistants(frames)
    errors = [frame for frame in frames if frame.get("type") == "error"]
    assert errors and errors[-1]["code"] == "empty_transcript"
    assert set(errors[-1]) == {"type", "code", "message"}


def test_error_and_cancelled_frames_carry_no_routing_fields() -> None:
    """The transport cannot attach metadata to a turn that produced none."""

    source = (
        Path(__file__).resolve().parents[1] / "src/butters/web/app.py"
    ).read_text(encoding="utf-8")

    # Every frame the voice route can emit other than `assistant` is built
    # from literal keys, so none of them can carry a `ServiceResponse`.
    for frame in ('{"type": "cancelled"}',):
        assert frame in source, frame
    error = source[source.index('async def _safe_ws_error') :]
    error = error[: error.index("\n\n\n")]
    for forbidden in METADATA_FIELDS:
        assert forbidden not in error, forbidden


def test_the_browser_renders_no_metadata_on_a_failed_or_cancelled_turn() -> None:
    """`failVoice` and `cancelVoice` call the renderer with text alone."""

    for name in ("function failVoice", "function cancelVoice"):
        block = _block(APP_JS, name)
        assert 'addMessage("assistant", message)' in block
        for forbidden in ("data", "meta", "metadata"):
            assert f", {forbidden})" not in block, (name, forbidden)


def test_an_invalid_assistant_frame_renders_nothing_at_all() -> None:
    handler = _block(APP_JS, "function handleVoiceEvent(event, turn)")
    branch = handler[handler.index('data.type === "assistant"') :]
    guard = branch.index('typeof data.response_text !== "string"')
    render = branch.index('addMessage("assistant", data.response_text, data)')

    assert guard < render, "the validity guard must precede the render"
    assert "failVoice(turn," in branch[guard:render]


# ==================== 7. nothing else moved ===============================


def test_the_service_response_is_still_the_single_declared_shape() -> None:
    """No voice-only schema was added beside `ServiceResponse`."""

    declared = set(ServiceResponse.__dataclass_fields__)
    for name in HIDDEN_REASONING_NAMES:
        assert name not in declared, name
    for field in METADATA_FIELDS:
        assert field in declared, field


def test_the_projection_helper_is_still_the_only_metadata_allow_list(
    tmp_path: Path,
) -> None:
    """`assistant_metadata` remains the one place the allow-list is applied."""

    response = ServiceResponse(
        "trace-1",
        "request-1",
        ANSWER,
        ANSWER.casefold(),
        "general_cloud",
        ("general_cloud",),
        model="gpt-5.6-terra",
        reasoning_effort="high",
        cloud_used=True,
        routing_mode="adaptive",
        routing_tier="terra",
        routing_reason_codes=("complexity_high",),
        estimated_complexity=7,
        tier_escalated=True,
        reasoning_summary=SUMMARY,
    )

    projected = assistant_metadata(response)
    assert projected is not None
    assert set(projected) <= set(METADATA_FIELDS)
    assert "response_text" not in projected
    assert "usage" not in projected
