"""Contracts the v2 interface has to keep.

The redesign moved every control on three pages and replaced a single
minified stylesheet with a layered one. Two classes of regression become easy
in that situation: a control that quietly stops existing, and a state that
quietly starts lying. Everything below guards one of those two.

Nothing here asserts that the pages are *pretty*. It asserts that they still
address the elements their scripts need, still reach every control, still
separate the claims the backend separates, and still colour a deliberate
configuration differently from a fault.
"""

from __future__ import annotations

import re

import pytest
from frontend_assets import (
    ALL_CSS,
    ASSET_ROOT,
    STATIC_ROOT,
    STYLESHEETS,
    contrast,
    declarations,
    token,
)

INDEX_HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
ADMIN_HTML = (STATIC_ROOT / "admin.html").read_text(encoding="utf-8")
PORTAL_HTML = (STATIC_ROOT / "portal.html").read_text(encoding="utf-8")
APP_JS = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
ADMIN_JS = (ASSET_ROOT / "admin.js").read_text(encoding="utf-8")
PORTAL_JS = (ASSET_ROOT / "portal.js").read_text(encoding="utf-8")

SURFACES = {
    "chat": (INDEX_HTML, APP_JS),
    "admin": (ADMIN_HTML, ADMIN_JS),
    "portal": (PORTAL_HTML, PORTAL_JS),
}


def _ids(document: str) -> set[str]:
    return set(re.findall(r'\bid="([A-Za-z0-9_-]+)"', document))


def _addressed(script: str) -> set[str]:
    """Every ``#id`` the script looks up, however it quotes it."""

    return set(re.findall(r"""querySelector\(\s*["']#([A-Za-z0-9_-]+)["']""", script))


# =========================== the stylesheet layer ===========================


def test_every_surface_loads_the_layered_stylesheet_and_nothing_stale() -> None:
    shared = ("tokens.css", "base.css", "components.css")
    for document, own in (
        (INDEX_HTML, "chat.css"),
        (ADMIN_HTML, "admin.css"),
        (PORTAL_HTML, "portal.css"),
    ):
        for sheet in (*shared, own):
            assert f'href="/assets/{sheet}"' in document, sheet
        # The two files the layering replaced must not linger in a link tag.
        assert "styles.css" not in document
        assert "auth.css" not in document
    assert not (ASSET_ROOT / "styles.css").exists()
    assert not (ASSET_ROOT / "auth.css").exists()


def test_only_the_token_layer_declares_raw_colour() -> None:
    """A hex literal anywhere else is a colour that cannot be re-themed."""

    for name, source in STYLESHEETS.items():
        if name == "tokens.css":
            continue
        stripped = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b", stripped), name


def test_the_token_layer_names_every_semantic_role() -> None:
    for name in (
        "--bg",
        "--surface",
        "--surface-raised",
        "--surface-sunken",
        "--border",
        "--text",
        "--text-secondary",
        "--text-muted",
        "--accent",
        "--success",
        "--warning",
        "--danger",
        "--info",
        "--neutral",
    ):
        assert token(name), name
    # Purpose, not appearance. A token named after its colour cannot survive
    # a palette change, which is exactly what this layer exists to allow.
    for forbidden in ("--green", "--sage", "--dark-", "--light-", "--olive"):
        assert forbidden not in STYLESHEETS["tokens.css"]


@pytest.mark.parametrize(
    "ink",
    ["--text", "--text-secondary", "--text-muted", "--accent", "--success",
     "--info", "--warning", "--danger"],
)
@pytest.mark.parametrize(
    "surface", ["--bg", "--surface", "--surface-raised", "--surface-sunken"]
)
def test_every_ink_is_legible_on_every_surface(ink: str, surface: str) -> None:
    assert contrast(token(ink), token(surface)) >= 4.5, (ink, surface)


def test_the_accent_ink_is_legible_on_the_accent() -> None:
    for background in ("--accent", "--accent-strong", "--warning"):
        assert contrast(token("--accent-ink"), token(background)) >= 4.5, background
    assert contrast(token("--text"), token("--accent-quiet")) >= 4.5


# ============================== accessibility ==============================


def _hover_selectors_outside_a_hover_query() -> list[str]:
    """Every ``:hover`` rule that is not guarded by a hover media query."""

    unguarded: list[str] = []
    for source in STYLESHEETS.values():
        body = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        depth_of_hover_query: list[int] = []
        depth = 0
        for chunk in re.split(r"([{}])", body):
            if chunk == "{":
                depth += 1
            elif chunk == "}":
                depth -= 1
                if depth_of_hover_query and depth < depth_of_hover_query[-1]:
                    depth_of_hover_query.pop()
            elif "@media" in chunk and "hover: hover" in chunk:
                depth_of_hover_query.append(depth + 1)
            elif ":hover" in chunk and not depth_of_hover_query:
                unguarded.append(chunk.strip())
    return unguarded


def test_nothing_is_reachable_by_hover_alone() -> None:
    """A phone has no hover. Every hover rule is an enhancement, not a state."""

    assert _hover_selectors_outside_a_hover_query() == []


def test_a_reader_who_asked_for_less_motion_gets_none() -> None:
    reduced = STYLESHEETS["base.css"]
    assert "@media (prefers-reduced-motion: reduce)" in reduced
    assert "animation-duration: 0.001ms !important" in reduced
    assert "transition-duration: 0.001ms !important" in reduced
    # Every keyframe animation the component layer starts is covered by that
    # global reset, and the two that convey state also have a still form.
    assert ALL_CSS.count("@media (prefers-reduced-motion: reduce)") >= 2


def test_focus_is_always_visible_and_never_merely_removed() -> None:
    focus = declarations(":focus-visible", STYLESHEETS["base.css"])
    assert focus["outline"].endswith("var(--accent)")
    assert focus["outline-offset"] == "2px"
    # Fields swap the outline for a ring; they do not drop it.
    field = declarations(
        "input:focus-visible,\ntextarea:focus-visible,\nselect:focus-visible",
        STYLESHEETS["components.css"],
    )
    assert field["box-shadow"] == "var(--focus-ring)"


def test_touch_targets_are_declared_once_and_are_large_enough() -> None:
    assert token("--tap-min") == "44px"
    # A coarse pointer raises every control to the touch minimum, rather than
    # each component remembering to do it.
    coarse = STYLESHEETS["tokens.css"]
    assert "@media (pointer: coarse)" in coarse
    assert "--control-height: var(--tap-min)" in coarse


def test_every_page_declares_a_viewport_that_fits_a_phone() -> None:
    for name, (document, _script) in SURFACES.items():
        assert 'name="viewport"' in document, name
        assert "width=device-width" in document, name
        assert "initial-scale=1" in document, name
        # Nothing may block a reader from zooming.
        assert "user-scalable=no" not in document, name
        assert "maximum-scale" not in document, name


# ============================ the DOM-hook contract ==========================


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_every_element_a_script_addresses_exists_on_its_page(surface: str) -> None:
    document, script = SURFACES[surface]
    missing = _addressed(script) - _ids(document)
    assert not missing, f"{surface}.js addresses ids that no longer exist: {missing}"


# The full set of Admin controls, frozen. A control may be moved to another
# panel; it may not stop existing because a redesign found it inconvenient.
ADMIN_CONTROLS = {
    # navigation and chrome
    "admin-nav", "panel-title", "admin-status",
    # overview
    "overview-grid",
    # diagnostics
    "refresh-traces", "trace-list", "session-list", "logs-view",
    # routing
    "routing-form", "routing-text", "route-override", "route-model",
    "route-effort", "route-output", "routing-result",
    # models and STT
    "model-status", "stt-file", "stt-test", "stt-result",
    # OpenAI credential
    "openai-card", "openai-summary", "openai-credential", "openai-test",
    "openai-set", "openai-remove", "openai-key-form", "openai-key",
    "openai-key-submit", "openai-key-cancel", "openai-remove-confirm",
    "openai-status",
    # chat model
    "chat-card", "chat-effective", "chat-provider", "chat-model",
    "chat-effort-field", "chat-effort", "chat-verbosity-field", "chat-verbosity",
    "chat-output-field", "chat-output", "chat-temperature-field",
    "chat-temperature", "chat-advanced", "chat-top-p-field", "chat-top-p",
    "chat-truncation-field", "chat-truncation", "chat-tool-calls-field",
    "chat-tool-calls", "chat-parallel-field", "chat-parallel",
    "chat-store-field", "chat-store", "chat-cache-field", "chat-cache",
    "chat-save", "chat-status",
    # speech
    "tts-card", "tts-effective", "tts-provider", "tts-model", "tts-voice",
    "tts-model-note", "tts-speed-field", "tts-speed", "tts-speed-value",
    "tts-instructions-field", "tts-instructions", "tts-instructions-note",
    "tts-advanced", "tts-format", "tts-save", "tts-preview", "tts-status",
    "voice-audio", "voice-presets",
    # skills and codex
    "skill-search", "create-skill-shortcut", "skill-list", "skill-detail",
    "skill-test-args", "skill-test", "skill-toggle", "skill-test-result",
    "codex-form", "codex-description", "codex-result", "codex-jobs",
    "codex-job-detail", "codex-run", "codex-approve", "codex-reject",
    # desktop
    "desktop-card", "desktop-summary", "desktop-status",
    "desktop-last-operation", "desktop-refresh", "desktop-ssh-test",
    "desktop-wake", "desktop-agent-status", "desktop-apps",
    "desktop-streaming", "desktop-vms", "desktop-compute-note",
    "desktop-result-summary", "desktop-result-details", "desktop-result",
    "desktop-shutdown", "desktop-shutdown-confirm", "desktop-shutdown-status",
    # NAS
    "nas-card", "nas-summary", "nas-status", "nas-last-operation",
    "nas-refresh", "nas-wake", "nas-action-status", "nas-shutdown",
    "nas-shutdown-confirm", "nas-shutdown-status",
    # portal enrollment
    "portal-identity", "portal-label", "portal-invite", "portal-invite-status",
    "portal-identity-list",
    # remaining panels
    "tool-list", "usage-view", "system-view", "security-view",
    "auth-admin-status", "admin-authenticate", "admin-lock", "add-passkey",
    "passkey-list", "action-admin-view", "capability-list",
}


def test_no_admin_control_was_lost_in_the_restructure() -> None:
    missing = ADMIN_CONTROLS - _ids(ADMIN_HTML)
    assert not missing, f"controls disappeared from Admin: {sorted(missing)}"


def test_every_admin_panel_is_reachable_and_named() -> None:
    panels = set(re.findall(r'id="panel-([a-z]+)" class="admin-panel', ADMIN_HTML))
    navigable = set(re.findall(r'data-panel="([a-z]+)"', ADMIN_HTML))
    assert panels == navigable, panels ^ navigable
    titled = set(
        re.findall(r"(\w+):\"", ADMIN_JS[ADMIN_JS.index("const titles = {") :][:2000])
    )
    assert navigable <= titled, navigable - titled


def test_the_mobile_picker_is_generated_from_the_sidebar() -> None:
    """One list of sections, rendered twice — never two lists to keep in step."""

    assert 'id="admin-nav-select"' in ADMIN_HTML
    builder = ADMIN_JS[ADMIN_JS.index("function buildMobileNav()") :]
    builder = builder[: builder.index("\nbuildMobileNav();")]
    assert "#admin-nav .nav-group" in builder
    assert "optgroup" in builder
    # The picker has no hard-coded options of its own.
    assert not re.search(r'new Option\("[A-Z]', builder)
    assert "showPanel(picker.value)" in builder


# ============================ status semantics ==============================


def _tone_map(script: str, name: str) -> dict[str, str]:
    block = script[script.index(f"const {name} = {{") :]
    block = block[: block.index("};")]
    return dict(re.findall(r"(\w+)\s*:\s*\"(\w+)\"", block))


def test_a_deliberate_configuration_is_never_toned_as_a_failure() -> None:
    """Off, dry run and "not enabled" are settings. They are not errors."""

    tones = _tone_map(ADMIN_JS, "AXIS_TONE")
    for state in ("off", "not_enabled", "unknown", "not_observed"):
        assert tones[state] == "muted", (state, tones[state])
    for state in ("dry_run", "observe"):
        assert tones[state] == "info", (state, tones[state])
    assert "bad" not in {tones[state] for state in ("off", "not_enabled", "dry_run")}


def test_the_status_vocabulary_has_a_distinct_tone_for_each_kind_of_claim() -> None:
    for tone in ("good", "bad", "warn", "info", "muted"):
        assert declarations(f".axis-{tone}"), tone
    # Tone is carried on one edge, so a grid of observations does not become a
    # grid of coloured boxes.
    cell = declarations(".axis-cell")
    assert cell["border-inline-start"].endswith("var(--neutral)")
    for tone in ("good", "bad", "warn", "info", "muted"):
        assert set(declarations(f".axis-{tone}")) <= {"border-inline-start-color"} | {
            "color"
        }


def test_a_desktop_that_is_switched_off_is_not_reported_as_a_fault() -> None:
    card = ADMIN_JS[ADMIN_JS.index("function overviewDesktop(value)") :]
    card = card[: card.index("\n}\n")]
    assert '"unreachable"?"muted"' in card.replace(" ", "")
    assert '"Off"' in card
    # And the text comes from the server's own summary, not from this file.
    assert "detail:value.summary" in card.replace(" ", "")


# ============================ overview honesty ==============================


def test_the_overview_reports_a_failed_read_as_a_failed_read() -> None:
    """A console that cannot reach a subsystem must not call it unhealthy."""

    block = ADMIN_JS[ADMIN_JS.index("function unavailableCard") :]
    block = block[: block.index("\n}\n")]
    assert 'state:"Unavailable"' in block
    assert 'tone:"muted"' in block
    assert "could not read the state" in block


def test_one_unreachable_subsystem_cannot_blank_the_others() -> None:
    block = ADMIN_JS[ADMIN_JS.index("async function refreshOverview()") :]
    block = block[: block.index("\n}\n")]
    assert "Promise.allSettled" in block
    # Five summary cards plus the raw service grid, each from its own result.
    assert block.count('status==="fulfilled"') == 6
    # Overview deliberately does not force a live NAS Agent round trip.
    assert '"/api/admin/tools/nas"' in block
    assert "refresh=1" not in block


# ================================ media =====================================


def test_the_media_panel_keeps_observed_calculated_and_policy_apart() -> None:
    for group in ("evidence-observed", "evidence-calculated", "evidence-policy"):
        assert group in ADMIN_HTML, group
        assert declarations(f".{group} .axis-cell"), group
    # Three visibly different surfaces, so the three kinds of claim cannot be
    # mistaken for one another.
    surfaces = {
        declarations(f".{group} .axis-cell")["background"]
        for group in ("evidence-observed", "evidence-calculated", "evidence-policy")
    }
    assert len(surfaces) == 3
    for node in ("media-observed", "media-calculated", "media-policy"):
        assert f'id="{node}"' in ADMIN_HTML, node


def test_the_nominal_jellyfin_rate_is_not_presented_as_a_wire_measurement() -> None:
    render = ADMIN_JS[ADMIN_JS.index("function renderMediaBandwidth(value)") :]
    render = render[: render.index("\n}\n")]
    assert "what Jellyfin says it is sending, not a wire measurement" in render
    assert "configured, not probed" in render


def test_dry_run_is_shown_truthfully_and_no_enforcement_control_exists() -> None:
    render = ADMIN_JS[ADMIN_JS.index("function renderMediaBandwidth(value)") :]
    render = render[: render.index("\n}\n")]
    assert '["Policy mode", String(value.policy_mode||"off")]' in render
    assert "It does not change any stream's bitrate" in render
    assert "No bitrate enforcement exists in this deployment." in render
    # There is no control anywhere that would suggest enforcement is available.
    for forbidden in ("enforce-", "Enable enforcement", "policy-mode-select"):
        assert forbidden not in ADMIN_HTML, forbidden
    assert "/api/admin/tools/nas/bandwidth" not in ADMIN_JS


# ============================ privacy boundaries ============================


def _stream_renderer(script: str) -> str:
    """Just the loop that turns one live stream into visible cells."""

    if script == "admin":
        block = ADMIN_JS[ADMIN_JS.index("function renderMediaStreams(sessions)") :]
        return block[: block.index("\n}\n")]
    block = PORTAL_JS[PORTAL_JS.index("const list=document.querySelector") :]
    return block[: block.index("\n}")]


@pytest.mark.parametrize("script", ["admin", "portal"])
def test_no_stream_listing_renders_an_identifier_or_a_device(script: str) -> None:
    """The agent reports more about a session than either page should show."""

    block = _stream_renderer(script)
    for field in ("session_id", "RemoteEndPoint", "position_ticks",
                  "stream.client", "stream.device", "bitrate_source"):
        assert field not in block, (script, field)
    # The rendered columns are the human ones only.
    assert "stream.user" in block and "stream.item" in block
    assert 'classification!=="remote"' in block or 'classification==="remote"' in block


def test_the_portal_still_names_no_host_endpoint_or_secret() -> None:
    for forbidden in ("192.168.", "/api/admin/", "api_key", "RemoteEndPoint",
                      "session_id", "csrf_token\"", "localStorage"):
        assert forbidden not in PORTAL_JS, forbidden


def test_the_portal_answers_its_one_question_before_it_shows_detail() -> None:
    """A household surface leads with "can I watch", not with telemetry."""

    hero = PORTAL_HTML.index("portal-hero")
    assert PORTAL_HTML.index('id="portal-open"') > hero
    assert hero < PORTAL_HTML.index('id="portal-bandwidth"')
    # The full measurement grid is behind a disclosure; the summary is not.
    assert 'id="portal-bandwidth-summary"' in PORTAL_HTML
    detail = PORTAL_HTML.index('id="portal-bandwidth-metrics"')
    assert PORTAL_HTML.rindex("<details", 0, detail) > PORTAL_HTML.index(
        'id="portal-bandwidth-summary"'
    )


# ========================= credentials and FRESH ============================


def test_the_credential_card_can_only_write_never_read_back() -> None:
    card = ADMIN_HTML[ADMIN_HTML.index('id="openai-card"') :]
    card = card[: card.index("</section>")]
    assert 'id="openai-key" type="password"' in card
    assert 'autocomplete="off"' in card
    assert "never stored in this browser" in card
    # No element on the page is a place a stored key could be rendered into.
    assert "api_key" not in ADMIN_HTML
    clearer = ADMIN_JS[ADMIN_JS.index("function clearKeyField()") :]
    assert 'field.value = ""' in clearer[:400]


def test_credential_mutation_still_requires_a_fresh_ceremony() -> None:
    submit = ADMIN_JS[ADMIN_JS.index("async function submitCredential(event)") :]
    submit = submit[: submit.index("\n}\n")]
    assert 'authenticatePurpose("openai_credential", "set")' in submit
    assert "fresh_grant: grant" in submit
    assert "confirm: true" in submit
    remove = ADMIN_JS[ADMIN_JS.index("async function removeCredential()") :]
    remove = remove[: remove.index("\n}\n")]
    assert 'authenticatePurpose("openai_credential", "remove")' in remove
    assert "fresh_grant: grant" in remove


def test_the_destructive_controls_still_freeze_a_server_named_action() -> None:
    for endpoint in (
        "/api/admin/tools/shutdown-nas",
        "/api/admin/tools/desktop/shutdown",
        "/api/admin/tools/wake-nas",
        "/api/admin/tools/desktop/wake",
        "/api/admin/tools/desktop/streaming",
    ):
        assert endpoint in ADMIN_JS, endpoint
    # And each still runs behind an explicit confirmation surface.
    for node in ("nas-shutdown-confirm", "desktop-shutdown-confirm",
                 "openai-remove-confirm"):
        assert f'id="{node}"' in ADMIN_HTML, node


def test_the_chat_surface_keeps_fresh_distinct_from_ordinary_elevation() -> None:
    block = APP_JS[APP_JS.index("function showPendingAction(plan)") :]
    block = block[: block.index("\n}\n")]
    assert 'plan.authentication === "fresh"' in block
    assert "fresh passkey confirmation bound to this exact action" in block


# ========================== catalogue-driven controls =======================


def test_no_model_voice_or_provider_is_typed_into_the_markup() -> None:
    """Every option comes from /api/admin/ai/catalog, at runtime."""

    for select in ("chat-provider", "chat-model", "tts-provider", "tts-model",
                   "tts-voice", "chat-effort", "chat-verbosity",
                   "chat-truncation", "tts-format", "route-model"):
        match = re.search(
            rf'<select id="{select}"[^>]*>(.*?)</select>', ADMIN_HTML, re.DOTALL
        )
        assert match, select
        assert match.group(1).strip() == "", f"{select} carries hard-coded options"
    for identifier in ("gpt-5", "gpt-4o", "tts-1", "cedar", "alloy", "piper"):
        assert identifier.lower() not in ADMIN_HTML.lower(), identifier


def test_an_unsupported_control_is_still_hidden_rather_than_disabled() -> None:
    capabilities = ADMIN_JS[ADMIN_JS.index("function renderChatCapabilities") :]
    capabilities = capabilities[: capabilities.index("\nfunction numberOrNull")]
    for field in ("chat-effort-field", "chat-verbosity-field",
                  "chat-temperature-field", "chat-top-p-field",
                  "chat-truncation-field", "chat-tool-calls-field"):
        assert f'show(document.querySelector("#{field}")' in capabilities, field


def test_advanced_sections_stay_collapsed_until_they_are_asked_for() -> None:
    for block in ('<details id="chat-advanced"', '<details id="tts-advanced"'):
        assert block in ADMIN_HTML
        assert f'{block} class="advanced-block" open' not in ADMIN_HTML
    summary = declarations(".advanced-block > summary")
    assert summary["cursor"] == "pointer"
    assert summary["min-height"] == "var(--control-height)"


# =============================== empty states ===============================


def test_every_collection_the_console_renders_has_an_empty_state() -> None:
    assert declarations(".empty-state")
    assert declarations(".skeleton")
    # The two collections the redesign introduced both use it.
    media = ADMIN_JS[ADMIN_JS.index("function renderMediaStreams(sessions)") :]
    media = media[: media.index("\n}\n")]
    assert "emptyState(" in media
    assert "No remote streams" in media
    refresh = ADMIN_JS[ADMIN_JS.index("async function refreshMedia()") :]
    assert "emptyState(" in refresh[: refresh.index("\n}\n")]


def test_a_busy_control_keeps_its_label_and_its_footprint() -> None:
    busy = declarations('[aria-busy="true"]::after')
    assert busy["content"] == '""'
    assert busy["flex"] == "none"
    # Chat marks the send button busy rather than swapping its contents.
    pending = APP_JS[APP_JS.index("function setPending(value)") :]
    pending = pending[: pending.index("\n}\n")]
    assert 'sendButton.setAttribute("aria-busy"' in pending
