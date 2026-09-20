"""Butters says plainly that its spoken voice is synthetic.

OpenAI's text-to-speech guidance requires disclosing that a generated voice is
not a human one. The wording is derived server-side from the provider actually
in effect, so the page cannot claim OpenAI after a switch back to the
on-device engine, nor fall silent after a switch to it.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

import httpx
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
INDEX = (STATIC / "index.html").read_text()
APP_JS = (STATIC / "assets/app.js").read_text()
from frontend_assets import declarations


class NoCloud:
    available = False


class Engine:
    initialization_seconds = 0.0

    def close(self):
        return None


def _service(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=("admin@example.com",),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    service = BetaAssistantService(
        settings, vocabulary, general_reasoner=NoCloud(), state_dir=tmp_path
    )
    app = create_app(settings, vocabulary, service, stt_engine_factory=Engine)
    return app, service


# Elements that never have an end tag, so they never open a nesting level.
_VOID = frozenset(
    (
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    )
)


class _ShellChildren(HTMLParser):
    """The direct children of ``main.chat-shell``, by their first class name."""

    def __init__(self) -> None:
        super().__init__()
        self.depth: int | None = None
        self.names: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if self.depth is None:
            if tag == "main" and "chat-shell" in (values.get("class") or ""):
                self.depth = 0
            return
        if self.depth == 0:
            classes = (values.get("class") or "").split()
            assert classes, f"<{tag}> in the chat shell carries no class to style"
            self.names.append(classes[0])
        if tag not in _VOID:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.depth is None:
            return
        self.depth -= 1
        if self.depth < 0:
            self.depth = None


def _shell_children(document: str) -> list[str]:
    parser = _ShellChildren()
    parser.feed(document)
    assert parser.names, "the chat shell has no children"
    return parser.names



# ------------------------------ the markup ---------------------------------


def test_the_chat_page_carries_a_disclosure_before_any_script_runs() -> None:
    """A safe, truthful default ships in the HTML itself."""

    assert 'id="voice-disclosure"' in INDEX
    element = re.search(r'<p id="voice-disclosure"[^>]*>([^<]+)</p>', INDEX)
    assert element, "the disclosure element is missing"
    assert "AI-generated" in element.group(1)
    assert "not a human voice" in element.group(1)
    assert 'role="note"' in element.group(0)


def test_the_disclosure_is_styled_and_does_not_break_the_layout() -> None:
    assert declarations(".voice-disclosure")

    # The shell is a grid with one track per child it lays out, and exactly
    # one flexible track. That single 1fr has to be the conversation: if any
    # other row could grow, the disclosure or the composer could be pushed
    # off the bottom of a phone screen, which is the defect this guards.
    shell = declarations(".chat-shell")
    tracks = shell["grid-template-rows"].split()
    assert tracks.count("1fr") == 1

    children = _shell_children(INDEX)
    # Every direct child except any the stylesheet takes out of flow.
    in_flow = [
        child
        for child in children
        if declarations(f".{child}").get("position") != "absolute"
    ]
    assert len(tracks) == len(in_flow), (tracks, in_flow)
    # And the flexible one is the conversation.
    assert in_flow[tracks.index("1fr")] == "conversation"


def test_the_page_takes_its_wording_from_the_server() -> None:
    assert "applyVoiceDisclosure(data.voice_disclosure)" in APP_JS
    # And keeps the safe default rather than blanking it if the server is quiet.
    body = APP_JS[APP_JS.index("function applyVoiceDisclosure") :]
    assert "if (text) node.textContent = text;" in body


def test_the_disclosure_is_not_repeated_per_synthesis() -> None:
    """Disclosure by nuisance is not the goal; it is shown once."""

    assert APP_JS.count("applyVoiceDisclosure(") == 2  # definition + one call
    # Nothing prepends it to spoken text or to each assistant message.
    assert "AI-generated" not in APP_JS


# ----------------------------- the server ----------------------------------


async def _session_payload(app) -> dict:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/api/session")
        assert response.status_code == 200, response.text
        return response.json()


async def _disclosure_follows_the_effective_provider(tmp_path, monkeypatch):
    app, service = _service(tmp_path, monkeypatch)

    payload = await _session_payload(app)
    local = payload["voice_disclosure"]
    assert local["ai_generated"] is True
    assert local["provider"] == "local"
    assert local["text"] == "Voice responses are AI-generated using an on-device model."

    service.ai.apply_speech(
        {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "cedar", "speed": 1.0}
    )
    payload = await _session_payload(app)
    cloud = payload["voice_disclosure"]
    assert cloud["provider"] == "openai"
    assert cloud["text"] == "Voice responses are AI-generated using OpenAI TTS."


def test_disclosure_follows_the_effective_provider(tmp_path, monkeypatch) -> None:
    asyncio.run(_disclosure_follows_the_effective_provider(tmp_path, monkeypatch))


async def _every_provider_discloses_something_truthful(tmp_path, monkeypatch):
    app, service = _service(tmp_path, monkeypatch)
    for provider, model, voice in (
        ("local", "local-piper", "kathleen"),
        ("openai", "tts-1", "alloy"),
        ("openai", "gpt-4o-mini-tts", "cedar"),
    ):
        service.ai.apply_speech(
            {"provider": provider, "model": model, "voice": voice, "speed": 1.0}
        )
        disclosure = (await _session_payload(app))["voice_disclosure"]
        assert disclosure["ai_generated"] is True
        assert "AI-generated" in disclosure["text"]
        assert disclosure["provider"] == provider


def test_every_provider_discloses_something_truthful(tmp_path, monkeypatch) -> None:
    asyncio.run(_every_provider_discloses_something_truthful(tmp_path, monkeypatch))


def test_the_disclosure_carries_no_configuration_detail(tmp_path, monkeypatch) -> None:
    """It names the provider, never the voice, model, key, or cost."""

    _app, service = _service(tmp_path, monkeypatch)
    service.ai.apply_speech(
        {"provider": "openai", "model": "gpt-4o-mini-tts", "voice": "cedar", "speed": 1.0}
    )
    disclosure = json.dumps(service.voice_disclosure())

    for leaked in ("cedar", "gpt-4o-mini-tts", "sk-", "api_key", "cost", "0.0018"):
        assert leaked not in disclosure, leaked
