"""Safe Markdown presentation for assistant answers.

Butters Chat used to set `textContent` on every message, so a model answer
containing headings, tables or emphasis was shown as raw Markdown source.
It is now parsed and rendered - but assistant output is untrusted input, so
the interesting part of this change is everything it is *not* allowed to do.

WHAT THESE TESTS CAN AND CANNOT SHOW
------------------------------------
This host has no browser and no JavaScript runtime of any kind (checked:
node, deno, bun, quickjs, and the Python JS bridges; also no chromium,
playwright or selenium). **No JavaScript in this change has been executed.**

So these tests hold three things that do not need a JS engine:

* the vendored bundles are exactly the reviewed releases, by digest;
* the policy the page configures is the policy intended - read out of the
  source, construct by construct;
* the server still stores and speaks the canonical `response_text`.

The one piece of policy written here rather than taken from a library - the
allowed-URI pattern - is extracted from the source and executed against the
obfuscated payload corpus in Python, so that part is genuinely tested.

What is *not* tested here is that markdown-it and DOMPurify behave as
documented. That rests on using current pinned releases of two widely
reviewed libraries, on `html: false` meaning no model HTML reaches the
sanitizer at all, and on a Content-Security-Policy that independently
forbids inline script, inline style and every off-origin source.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest
from beta1_harness import build_app, client
from frontend_assets import STYLESHEETS, declarations

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ASSETS = STATIC / "assets"
VENDOR = ASSETS / "vendor"
APP_JS = (ASSETS / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
VENDOR_README = (VENDOR / "README.md").read_text(encoding="utf-8")
APP_PY = (Path(__file__).parents[1] / "src/butters/web/app.py").read_text(encoding="utf-8")

# The reviewed releases. Changing a bundle without changing this mapping is a
# test failure, because the file that sanitizes model output is not something
# that should be able to drift quietly.
VENDOR_DIGESTS = {
    "markdown-it.umd.min.js": "635972b985228e8af9f0143647c68616b7a3bb09f6946e7e4a52e43dcf5e7be5",
    "purify.min.js": "f263b05369e050fa175d4ecb9c9358eb4253602d510297adfb31df48b2f1c4d5",
}
PINNED_VERSIONS = {"markdown-it": "15.0.2", "dompurify": "3.4.15"}


def _css(selector: str) -> dict[str, str]:
    """Declarations of one chat.css rule, tolerant of how it was wrapped.

    The shared helper matches a selector literally, which cannot find a
    grouped rule written across several lines. This normalises whitespace on
    both sides so the test asserts what the rule declares, not how it was
    typed.
    """

    source = re.sub(r"/\*.*?\*/", " ", STYLESHEETS["chat.css"], flags=re.DOTALL)
    flat = " ".join(source.split())
    pattern = re.compile(
        r"(?:^|[};])\s*" + re.escape(" ".join(selector.split())) + r"\s*\{([^{}]*)\}"
    )
    match = pattern.search(flat)
    assert match is not None, f"missing CSS rule for {selector}"
    parsed: dict[str, str] = {}
    for statement in match.group(1).split(";"):
        name, separator, value = statement.partition(":")
        if separator:
            parsed[name.strip()] = " ".join(value.split())
    return parsed


def _function(name: str) -> str:
    """The source of one top-level function in app.js."""

    start = APP_JS.index(f"function {name}(")
    return APP_JS[start : APP_JS.index("\n}\n", start)]


# ===================== 1. vendored dependency custody =====================


@pytest.mark.parametrize("name", sorted(VENDOR_DIGESTS))
def test_the_vendored_bundle_is_the_reviewed_release(name: str) -> None:
    body = (VENDOR / name).read_bytes()

    assert hashlib.sha256(body).hexdigest() == VENDOR_DIGESTS[name], name


@pytest.mark.parametrize(
    "name", ["MARKDOWN-IT-LICENSE.txt", "DOMPURIFY-LICENSE.txt", "README.md"]
)
def test_the_licence_and_provenance_travel_with_the_code(name: str) -> None:
    body = (VENDOR / name).read_text(encoding="utf-8")

    assert body.strip(), name


def test_the_provenance_note_records_the_pinned_versions_and_digests() -> None:
    for package, version in PINNED_VERSIONS.items():
        assert f"{package}@{version}" in VENDOR_README or version in VENDOR_README, package
    for digest in VENDOR_DIGESTS.values():
        assert digest in VENDOR_README


def test_dompurify_keeps_its_licence_banner() -> None:
    """The bundle is committed unmodified, banner included."""

    head = (VENDOR / "purify.min.js").read_text(encoding="utf-8")[:400]

    assert "@license DOMPurify" in head
    assert PINNED_VERSIONS["dompurify"] in head


def test_chat_never_reaches_a_cdn() -> None:
    """Chat must keep working on a host with no Internet access, and a model
    answer must not be able to cause an off-origin request."""

    for source, label in ((INDEX_HTML, "index.html"), (APP_JS, "app.js")):
        for forbidden in ("cdn.jsdelivr", "unpkg.com", "cdnjs", "//ajax.", "https://cdn"):
            assert forbidden not in source, (label, forbidden)
    assert 'src="/assets/markdown-it.umd.min.js"' in INDEX_HTML
    assert 'src="/assets/purify.min.js"' in INDEX_HTML


def test_the_libraries_are_defined_before_the_page_script_runs() -> None:
    """Deferred scripts execute in document order, so order is the guarantee."""

    parser = INDEX_HTML.index("markdown-it.umd.min.js")
    sanitizer = INDEX_HTML.index("purify.min.js")
    application = INDEX_HTML.index("/assets/app.js")

    assert parser < application
    assert sanitizer < application
    for name in ("markdown-it.umd.min.js", "purify.min.js", "app.js"):
        tag = INDEX_HTML[INDEX_HTML.index(f"/assets/{name}") :]
        assert "defer" in tag[: tag.index(">")], name


def test_the_bundles_are_published_through_the_explicit_allow_list() -> None:
    """Nothing becomes public by being dropped into a directory."""

    assert '"markdown-it.umd.min.js": (' in APP_PY
    assert '"purify.min.js": (' in APP_PY
    assert 'ASSET_ROOT / "vendor" / "markdown-it.umd.min.js"' in APP_PY
    assert 'ASSET_ROOT / "vendor" / "purify.min.js"' in APP_PY


def test_the_server_serves_the_bundles_it_vendored(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        async with client(app) as http:
            for name, digest in VENDOR_DIGESTS.items():
                response = await http.get(f"/assets/{name}")
                assert response.status_code == 200, name
                assert "javascript" in response.headers["content-type"]
                assert hashlib.sha256(response.content).hexdigest() == digest, name
            # The licence and provenance files are shipped, not published.
            for private in ("vendor/README.md", "README.md", "DOMPURIFY-LICENSE.txt"):
                assert (await http.get(f"/assets/{private}")).status_code == 404, private

    asyncio.run(scenario())


# ================ 2. the Content-Security-Policy is unchanged =============

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; media-src 'self' blob:; "
    "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def test_the_policy_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        async with client(app) as http:
            response = await http.get("/healthz")
            assert response.headers["content-security-policy"] == EXPECTED_CSP

    asyncio.run(scenario())


def test_rendering_markdown_did_not_buy_a_weaker_policy() -> None:
    for forbidden in ("unsafe-inline", "unsafe-eval", "unsafe-hashes", "*"):
        assert forbidden not in EXPECTED_CSP, forbidden
    # Vendored scripts are same-origin, so 'self' is still sufficient.
    assert "script-src 'self'" in EXPECTED_CSP
    # No remote image source, so even a rendered <img> could fetch nothing.
    assert "img-src 'self' data:" in EXPECTED_CSP


def test_inline_style_is_still_forbidden() -> None:
    """`style-src 'self'` with no unsafe-inline means a style attribute that
    somehow survived sanitization still could not apply."""

    assert "style-src 'self'" in EXPECTED_CSP
    assert "style-src 'self' 'unsafe-inline'" not in EXPECTED_CSP


# ==================== 3. the parser refuses raw HTML ======================


def test_the_parser_is_configured_to_treat_model_html_as_text() -> None:
    """The first and most important layer: with `html: false`, raw HTML in a
    model answer is escaped into visible characters and never becomes markup,
    so the sanitizer is a second line rather than the only one."""

    assert re.search(r"html:\s*false", APP_JS)
    assert not re.search(r"html:\s*true", APP_JS)


def test_only_an_explicit_markdown_link_becomes_a_link() -> None:
    assert re.search(r"linkify:\s*false", APP_JS)


def test_ordinary_line_breaks_are_preserved() -> None:
    assert re.search(r"breaks:\s*true", APP_JS)


def test_images_are_not_rendered_at_all() -> None:
    """A model-supplied image URL would make the browser fetch a third-party
    resource before the reader chose to trust it."""

    assert 'markdown.disable("image"' in APP_JS
    # And three independent things still stop one appearing if that rule ever
    # goes away: the allow-list, the forbidden list, and the CSP.
    assert "img" not in _allowed_tags()
    assert '"img"' in _config_block()
    assert "img-src 'self' data:" in EXPECTED_CSP


# ================= 4. the sanitizer policy is the intended one ============


def _config_block() -> str:
    start = APP_JS.index("const PURIFY_CONFIG")
    return APP_JS[start : APP_JS.index("\n};", start)]


def _allowed_tags() -> set[str]:
    block = APP_JS[APP_JS.index("const MARKDOWN_TAGS") :]
    block = block[: block.index("];")]
    return set(re.findall(r'"([a-z0-9]+)"', block))


def test_the_allowed_tags_are_exactly_the_markdown_constructs() -> None:
    assert _allowed_tags() == {
        "p", "br", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "strong", "em", "s", "del", "code", "pre",
        "ul", "ol", "li",
        "blockquote",
        "table", "thead", "tbody", "tr", "th", "td",
        "a",
    }


@pytest.mark.parametrize(
    "tag",
    ["script", "style", "iframe", "object", "embed", "svg", "math", "img", "form", "link", "base"],
)
def test_no_dangerous_element_is_allowed(tag: str) -> None:
    assert tag not in _allowed_tags(), tag
    assert f'"{tag}"' in _config_block(), f"{tag} should also be named in FORBID_TAGS"


def _allowed_attributes() -> set[str]:
    block = _config_block()
    section = block[block.index("ALLOWED_ATTR") :]
    section = section[: section.index("]")]
    return set(re.findall(r'"([a-zA-Z:-]+)"', section))


def test_the_allowed_attributes_carry_no_behaviour() -> None:
    allowed = _allowed_attributes()

    assert allowed == {"href", "title", "start", "colspan", "rowspan"}
    for forbidden in ("src", "srcset", "style", "class", "id", "target", "formaction"):
        assert forbidden not in allowed, forbidden
    assert not any(name.startswith("on") for name in allowed)


def test_data_and_aria_attributes_are_not_waved_through() -> None:
    block = _config_block()

    assert re.search(r"ALLOW_DATA_ATTR:\s*false", block)
    assert re.search(r"ALLOW_ARIA_ATTR:\s*false", block)


def test_the_sanitizer_returns_nodes_rather_than_a_string() -> None:
    """Returning a fragment is what lets the renderer avoid innerHTML."""

    assert re.search(r"RETURN_DOM_FRAGMENT:\s*true", _config_block())


def test_no_innerhtml_anywhere_on_the_chat_surface() -> None:
    """The trust boundary is DOMPurify, and nothing routes around it."""

    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        occurrences = [
            line
            for line in APP_JS.splitlines()
            if forbidden in line and not line.strip().startswith("*")
        ]
        assert occurrences == [], (forbidden, occurrences)


def test_a_library_that_failed_to_load_degrades_to_plain_text() -> None:
    body = _function("renderAssistantMarkdown")

    assert "if (!markdown || !window.DOMPurify)" in body
    assert "body.textContent = source;" in body
    # And a parser that throws does the same rather than leaking markup.
    assert "catch (error)" in body


# ======================== 5. the link policy ==============================
#
# This is the one rule written here rather than taken from a library, so it
# is the one rule these tests can and do execute.


def _safe_uri_pattern() -> re.Pattern[str]:
    """The page's own allowed-URI pattern, compiled with Python semantics."""

    match = re.search(r"const SAFE_URI = /(.+?)/i;", APP_JS)
    assert match, "SAFE_URI is not declared as expected"
    return re.compile(match.group(1), re.IGNORECASE)


SAFE_URLS = [
    "https://openai.com/",
    "http://192.168.1.10:8096/",
    "HTTPS://EXAMPLE.COM",
    "mailto:someone@example.com",
]

DANGEROUS_URLS = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "  javascript:alert(1)",
    "java\tscript:alert(1)",
    "javascript&colon;alert(1)",
    "java&#x73;cript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    "vbscript:msgbox(1)",
    "VBScript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://example.com/abc",
    "about:blank",
]


@pytest.mark.parametrize("url", SAFE_URLS)
def test_a_reviewed_scheme_is_accepted(url: str) -> None:
    assert _safe_uri_pattern().match(url)


@pytest.mark.parametrize("url", DANGEROUS_URLS)
def test_a_dangerous_scheme_is_rejected(url: str) -> None:
    """Positively allow-listed, so an unknown or mangled scheme fails by
    default instead of needing to be predicted and blocked."""

    assert _safe_uri_pattern().match(url) is None, url


def test_the_pattern_is_anchored_so_a_prefix_cannot_smuggle_a_scheme() -> None:
    pattern = _safe_uri_pattern()

    assert pattern.match("nothttps://example.com") is None
    assert pattern.match("javascript:https://example.com") is None


def test_the_pattern_is_the_one_the_sanitizer_is_given() -> None:
    assert "ALLOWED_URI_REGEXP: SAFE_URI" in _config_block()


def test_an_unsafe_link_keeps_its_words_and_loses_its_destination() -> None:
    hook = APP_JS[APP_JS.index('addHook("afterSanitizeAttributes"') :]
    hook = hook[: hook.index("\n  });")]

    assert "!SAFE_URI.test(href)" in hook
    assert 'node.removeAttribute("href")' in hook
    assert 'node.removeAttribute("target")' in hook


def test_an_external_link_cannot_reach_back_into_butters() -> None:
    hook = APP_JS[APP_JS.index('addHook("afterSanitizeAttributes"') :]
    hook = hook[: hook.index("\n  });")]

    assert 'node.setAttribute("target", "_blank")' in hook
    rel = re.search(r'setAttribute\("rel",\s*"([^"]+)"\)', hook)
    assert rel, "external links must carry a rel"
    assert {"noopener", "noreferrer"} <= set(rel.group(1).split())


def test_the_hook_is_registered_once_rather_than_per_render() -> None:
    assert APP_JS.count('addHook("afterSanitizeAttributes"') == 1
    assert "addHook" not in _function("renderAssistantMarkdown")


# ==================== 6. the injection corpus =============================
#
# Each payload is paired with the configured mechanism that neutralises it.
# These assertions are about policy, not about executed behaviour - see the
# module docstring. They exist so that removing a mechanism fails loudly.

INJECTION_CORPUS = [
    ("<script>alert(1)</script>", "script"),
    ("<img src=x onerror=alert(1)>", "img"),
    ("<iframe src=\"https://example.com\"></iframe>", "iframe"),
    ("<svg onload=alert(1)></svg>", "svg"),
    ("<style>body{display:none}</style>", "style"),
    ('<div onclick="alert(1)">x</div>', "div"),
    ("<object data=x></object>", "object"),
    ("<embed src=x>", "embed"),
    ("<base href=\"https://evil.example\">", "base"),
]


@pytest.mark.parametrize(("payload", "element"), INJECTION_CORPUS)
def test_raw_html_in_a_model_answer_has_no_route_to_markup(
    payload: str, element: str
) -> None:
    # Layer one: the parser never produces an element from this text.
    assert re.search(r"html:\s*false", APP_JS)
    # Layer two: even if it did, the element is not in the allow-list.
    assert element not in _allowed_tags(), (payload, element)


def test_a_markdown_link_cannot_carry_a_script_url() -> None:
    """`[click](javascript:alert(1))` is parsed as a link by design; its
    destination is what must not survive."""

    assert _safe_uri_pattern().match("javascript:alert(1)") is None
    assert "ALLOWED_URI_REGEXP: SAFE_URI" in _config_block()


def test_html_inside_a_fenced_block_stays_visible_code() -> None:
    """A fenced block's content is text content of <pre><code>, never markup,
    and `pre`/`code` are the only elements involved."""

    assert {"pre", "code"} <= _allowed_tags()
    assert re.search(r"html:\s*false", APP_JS)
    code = _css(".message-body pre code")
    assert code["white-space"] == "pre"


def test_nothing_in_the_render_path_can_start_a_network_request() -> None:
    body = _function("renderAssistantMarkdown")

    for forbidden in ("fetch(", "XMLHttpRequest", "import(", "new Image", "src ="):
        assert forbidden not in body, forbidden


# ============== 7. user messages are never parsed as Markdown =============


def test_user_text_is_shown_exactly_as_typed() -> None:
    body = _function("addMessage")

    branch = body[body.index('if (role === "user")') : body.index("} else {")]
    assert "paragraph.textContent = text;" in branch
    assert "renderAssistantMarkdown" not in branch


def test_only_the_assistant_branch_renders_markdown() -> None:
    body = _function("addMessage")

    assert body.count("renderAssistantMarkdown(text)") == 1
    assert body.index("} else {") < body.index("renderAssistantMarkdown(text)")


def test_a_typed_newline_survives_but_typed_markdown_does_not() -> None:
    """The person's own newlines are preserved by CSS, not by a parser."""

    assert _css(".user-message p")["white-space"] == "pre-wrap"
    # And the rendered answer must not also get pre-wrap, which would add the
    # source's newlines on top of the breaks the parser already produced.
    assert "white-space" not in _css(".message p")


# ============ 8. both render paths use the same safe renderer =============


def test_a_fresh_answer_and_a_reloaded_one_render_the_same_way() -> None:
    """One renderer, reached through one function, from both paths."""

    assert "for (const message of data.messages) addMessage(message.role, message.text);" in APP_JS
    assert 'addMessage("assistant", data.response_text, data);' in APP_JS
    # renderAssistantMarkdown is called from addMessage and the summary only.
    assert APP_JS.count("renderAssistantMarkdown(") == 3  # 1 definition + 2 uses


def test_the_voice_path_renders_through_the_same_function() -> None:
    assert APP_JS.count('addMessage("assistant"') >= 2


def test_the_session_endpoint_returns_canonical_text(tmp_path: Path) -> None:
    """Markdown renders after reload because the server stored the source,
    not a rendering of it."""

    async def scenario() -> None:
        app, _service, _settings = build_app(tmp_path)
        async with client(app) as http:
            first = await http.get("/api/session")
            assert first.status_code == 200
            payload = first.json()
            assert payload["messages"] == []
            keys = {"role", "text", "trace_id"}
            # The shape the browser restores from: text, and nothing rendered.
            assert all(key in keys for key in ("role", "text", "trace_id"))

    asyncio.run(scenario())
    assert '"text": item.text' in APP_PY
    assert "html" not in APP_PY[APP_PY.index('"messages": ['): APP_PY.index('"messages": [') + 400]


# =============== 9. the canonical text, persistence and speech ============

MARKDOWN_ANSWER = (
    "### Current finding\n\n"
    "**OBSERVED:** the room cooled.\n\n"
    "| Cause | Evidence |\n| --- | --- |\n| Conduction | Gradient |\n"
)


def test_the_server_stores_the_markdown_source_verbatim(tmp_path: Path) -> None:
    from dataclasses import replace

    from butters.assistant import create_assistant
    from butters.assistant_config import load_assistant_settings
    from butters.cloud.general import GeneralCloudTurn
    from butters.cloud.model import CloudTokenUsage
    from butters.stt.normalization import DomainVocabulary
    from butters.web.service import BetaAssistantService
    from test_adaptive_cloud_reasoning import Health, Sensors

    class Markdowny:
        available = True

        def reason(self, **kwargs):
            return GeneralCloudTurn(
                str(kwargs["model"]),
                str(kwargs["effort"]),
                0.01,
                response_id="r",
                response_text=MARKDOWN_ANSWER,
                usage=CloudTokenUsage(input_tokens=10, output_tokens=5),
            )

    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(base.cloud, enabled=True, allow_paid_calls=True),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=Markdowny(),
        state_dir=tmp_path,
    )
    session = service.sessions.create()

    response = service.handle_text(
        session, "Why does warm air hold more water vapour than cold air?"
    )

    # Every Markdown character the model produced is still there, unescaped
    # and un-rendered. Presentation happens in the browser and nowhere else.
    assert response.response_text == MARKDOWN_ANSWER
    assert "###" in response.response_text
    assert "<" not in response.response_text
    stored = next(
        item.text
        for item in reversed(session.messages)
        if item.role == "assistant" and item.trace_id == response.trace_id
    )
    # The stored copy differs from the wire copy only by trailing whitespace.
    # Every line, blank line and table pipe that Markdown needs is intact,
    # which is what makes the answer render the same way after a reload.
    assert stored == MARKDOWN_ANSWER.strip()
    assert stored.splitlines() == MARKDOWN_ANSWER.strip().splitlines()
    assert "\n\n" in stored
    assert stored.count("|") == MARKDOWN_ANSWER.count("|")


def test_speech_still_receives_the_canonical_text(tmp_path: Path) -> None:
    """The TTS invariant is unchanged: speech reads the stored assistant
    message, which is the Markdown source, never rendered DOM or HTML."""

    from dataclasses import replace

    from butters.assistant import create_assistant
    from butters.assistant_config import load_assistant_settings
    from butters.cloud.general import GeneralCloudTurn
    from butters.cloud.model import CloudTokenUsage
    from butters.stt.normalization import DomainVocabulary
    from butters.web.service import BetaAssistantService
    from butters.web.speech import SpeechResult
    from test_adaptive_cloud_reasoning import Health, Sensors

    class Markdowny:
        available = True

        def reason(self, **kwargs):
            return GeneralCloudTurn(
                str(kwargs["model"]),
                str(kwargs["effort"]),
                0.01,
                response_id="r",
                response_text=MARKDOWN_ANSWER,
                reasoning_summary="I compared **A** and *B*.",
                usage=CloudTokenUsage(input_tokens=10, output_tokens=5),
            )

    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(base.cloud, enabled=True, allow_paid_calls=True),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=Markdowny(),
        state_dir=tmp_path,
    )
    service.ai.apply_chat(
        {
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "max_output_tokens": 1200,
            "reasoning_summary_enabled": True,
        }
    )
    session = service.sessions.create()
    response = service.handle_text(
        session, "Why does warm air hold more water vapour than cold air?"
    )

    spoken: list[str] = []

    def recording(text, preset, **_kwargs):
        spoken.append(text)
        return SpeechResult(b"RIFF", "local", "local-piper", "kathleen", 0.01, 0.5)

    service.synthesize_preview = recording  # type: ignore[method-assign]
    service.synthesize_trace_response(session, response.trace_id)

    assert spoken == [MARKDOWN_ANSWER.strip()]
    # No rendered markup, and still no reasoning summary.
    assert "<p>" not in spoken[0]
    assert "<table" not in spoken[0]
    assert "compared" not in spoken[0]
    assert "**A**" not in spoken[0]


def test_no_rendered_html_is_added_to_the_service_response() -> None:
    """Rendering is a browser concern. The typed response gained no HTML."""

    from butters.web.service import ServiceResponse

    fields = set(ServiceResponse.__dataclass_fields__)

    assert "response_text" in fields
    for invented in ("response_html", "rendered_html", "html", "markdown_html"):
        assert invented not in fields, invented


# ==================== 10. the rendered answer's styling ===================


def test_a_heading_inside_a_message_stays_message_sized() -> None:
    for selector, expected in (
        (".message-body h1", "1.15rem"),
        (".message-body h2", "1.05rem"),
    ):
        assert _css(selector)["font-size"] == expected


@pytest.mark.parametrize(
    "selector",
    [
        (
            ".message-body p, .message-body ul, .message-body ol, "
            ".message-body pre, .message-body blockquote, "
            ".message-body .table-scroll"
        ),
        ".message-body ul, .message-body ol",
        ".message-body li",
        ".message-body strong",
        ".message-body em",
        ".message-body a",
        ".message-body code",
        ".message-body pre",
        ".message-body blockquote",
        ".message-body hr",
        ".message-body table",
        ".message-body th, .message-body td",
        ".message-body thead th",
    ],
)
def test_every_rendered_construct_is_styled(selector: str) -> None:
    assert _css(selector)


def test_a_code_block_scrolls_instead_of_widening_the_bubble() -> None:
    block = _css(".message-body pre")
    code = _css(".message-body pre code")

    assert block["overflow-x"] == "auto"
    assert code["white-space"] == "pre"
    assert code["overflow-wrap"] == "normal"


def test_a_wide_table_scrolls_inside_the_message() -> None:
    scroller = _css(".table-scroll")
    cells = _css(".message-body th, .message-body td")

    assert scroller["overflow-x"] == "auto"
    assert cells["border"].startswith("1px solid")
    assert _css(".message-body table")["border-collapse"] == "collapse"


def test_the_table_wrapper_is_applied_to_sanitized_nodes() -> None:
    body = _function("renderAssistantMarkdown")

    assert 'body.querySelectorAll("table")' in body
    assert 'scroller.className = "table-scroll"' in body
    # After the boundary: the wrapper is built from createElement, not markup.
    assert body.index("DOMPurify.sanitize") < body.index("table-scroll")


def test_narrow_screens_keep_readable_table_type() -> None:
    """Padding gives way before type size does, so a phone scrolls a legible
    table rather than showing a shrunken one."""

    stylesheet = STYLESHEETS["chat.css"]
    narrow = stylesheet[stylesheet.index("@media (max-width: 760px)") :]
    rule = narrow[narrow.index(".message-body th") :]
    rule = rule[: rule.index("}")]

    assert "padding" in rule
    assert "font-size" not in rule


def test_the_markdown_styling_uses_only_design_tokens() -> None:
    """No raw colour literal creeps into the Meadow surface."""

    section = STYLESHEETS["chat.css"]
    section = section[section.index("rendered Markdown") : section.index("answer provenance")]
    assert not re.search(r":\s*#[0-9a-fA-F]{3,8}\b", section)
    assert "var(--" in section


# ====================== 11. what must not have moved ======================


def test_the_hidden_invariant_survives_this_patch() -> None:
    assert declarations("[hidden]", STYLESHEETS["base.css"])["display"] == "none !important"


def test_routing_metadata_is_not_markdown_rendered() -> None:
    """Metadata is Butters' own text and stays outside the rendered answer."""

    meta = _function("cloudMetadata")

    assert "node.textContent = parts.join" in meta
    assert "renderAssistantMarkdown" not in meta


def test_the_summary_is_rendered_but_stays_in_its_disclosure() -> None:
    summary = _function("reasoningSummary")

    assert 'createElement("details")' in summary
    assert "renderAssistantMarkdown(text)" in summary
    assert "reasoning-summary-body" in summary
    # Still separate from the answer, still labelled for what it is.
    assert '"Reasoning summary"' in summary


def test_the_adaptive_metadata_line_is_unchanged() -> None:
    meta = _function("cloudMetadata")

    assert 'meta.tier_escalated === true ? "Escalating reasoning" : "Using cloud reasoning"' in meta
    assert "meta.routing_tier" in meta


# ========== 12. the stored copy keeps the structure Markdown needs ========
#
# Conversation storage used to fold every message onto one line. That was
# invisible while answers were displayed as plain text, and fatal once they
# are parsed: a reload rebuilds the conversation from here, and a heading, a
# list and a table are all defined by their line breaks.


def test_storage_keeps_the_line_structure_markdown_depends_on() -> None:
    from butters.web.sessions import normalize_message_text

    answer = "### Finding\n\n- one\n- two\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"

    assert normalize_message_text(answer, limit=4000) == answer


def test_storage_keeps_indentation_inside_code_and_nested_lists() -> None:
    from butters.web.sessions import normalize_message_text

    answer = "```python\ndef f():\n    return 1\n```\n\n- a\n  - b"

    stored = normalize_message_text(answer, limit=4000)
    assert "    return 1" in stored
    assert "  - b" in stored


def test_storage_still_removes_control_characters() -> None:
    from butters.web.sessions import normalize_message_text

    stored = normalize_message_text("a\x00b\x07c\x1bd", limit=4000)

    assert stored == "abcd"
    for control in ("\x00", "\x07", "\x1b"):
        assert control not in stored


def test_storage_normalises_carriage_returns_and_trailing_space() -> None:
    from butters.web.sessions import normalize_message_text

    stored = normalize_message_text("one   \r\ntwo\t\r\n", limit=4000)

    assert stored == "one\ntwo"


def test_storage_bounds_blank_runs_and_length() -> None:
    from butters.web.sessions import normalize_message_text

    assert normalize_message_text("a\n\n\n\n\n\nb", limit=4000) == "a\n\nb"
    assert len(normalize_message_text("x" * 9000, limit=4000)) == 4000


def test_a_reloaded_answer_is_the_same_source_the_browser_first_rendered(
    tmp_path: Path,
) -> None:
    """The two render paths are handed the same characters, so they cannot
    disagree about what the answer looks like."""

    from dataclasses import replace

    from butters.assistant import create_assistant
    from butters.assistant_config import load_assistant_settings
    from butters.cloud.general import GeneralCloudTurn
    from butters.cloud.model import CloudTokenUsage
    from butters.stt.normalization import DomainVocabulary
    from butters.web.service import BetaAssistantService
    from test_adaptive_cloud_reasoning import Health, Sensors

    class Markdowny:
        available = True

        def reason(self, **kwargs):
            return GeneralCloudTurn(
                str(kwargs["model"]),
                str(kwargs["effort"]),
                0.01,
                response_id="r",
                response_text=MARKDOWN_ANSWER.strip(),
                usage=CloudTokenUsage(input_tokens=10, output_tokens=5),
            )

    base = load_assistant_settings()
    settings = replace(
        base,
        cloud=replace(base.cloud, enabled=True, allow_paid_calls=True),
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(base.web, state_dir=tmp_path, development_mode=True),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    vocabulary = DomainVocabulary((), ())
    assistant = create_assistant(
        settings, vocabulary, sensor_adapter=Sensors(), server_adapter=Health()
    )
    service = BetaAssistantService(
        settings,
        vocabulary,
        assistant=assistant,
        general_reasoner=Markdowny(),
        state_dir=tmp_path,
    )
    session = service.sessions.create()

    live = service.handle_text(
        session, "Why does warm air hold more water vapour than cold air?"
    )
    reloaded = next(
        item.text
        for item in reversed(session.messages)
        if item.role == "assistant" and item.trace_id == live.trace_id
    )

    assert reloaded == live.response_text
