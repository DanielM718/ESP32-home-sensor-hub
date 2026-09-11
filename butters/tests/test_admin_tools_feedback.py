"""Regression tests for Admin -> Tools interaction feedback.

The complaint these answer was that the controls "don't have much feedback --
it kind of feels like I'm pressing an image": the backend was doing real
authorized asynchronous work and the page said almost nothing until it came
back, then printed a raw job object.

These are static contract tests over the three files that make up the panel.
They deliberately pin behaviour, not aesthetics: that every interaction state
exists, that none of them is carried by colour alone, that the raw diagnostic
result is kept rather than discarded, and that the truthful parts of the
stabilization work -- independent state axes, the four-state control model,
observed-only progress -- were not flattened in the name of looking tidier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ADMIN_JS = STATIC / "assets/admin.js"
ADMIN_HTML = STATIC / "admin.html"
ADMIN_CSS = STATIC / "assets/styles.css"


@pytest.fixture(scope="module")
def js() -> str:
    return ADMIN_JS.read_text()


@pytest.fixture(scope="module")
def html() -> str:
    return ADMIN_HTML.read_text()


@pytest.fixture(scope="module")
def css() -> str:
    return ADMIN_CSS.read_text()


@pytest.fixture(scope="module")
def tools(js: str) -> str:
    """Only the Tools panel, so unrelated older panels are not asserted on."""

    return js[js.index("Tools panel ===") :]


# ======================================================= interaction states ===


def test_controls_have_every_interaction_state(css: str):
    """Normal, hover, pressed, focus, disabled, busy and outcome."""

    for selector in (
        ".admin-main .primary-button:hover:not([disabled])",
        ".admin-main .secondary-button:hover:not([disabled])",
        ".admin-main button:active:not([disabled])",
        ".admin-main button:focus-visible",
        ".admin-main button[disabled]",
        '.admin-main button[data-busy="true"]',
        '.admin-main button[data-outcome="success"]',
        '.admin-main button[data-outcome="failure"]',
        '.admin-main button[data-control-state="requires_authorization"]',
        '.admin-main button[data-control-state="not_configured"]',
    ):
        assert selector in css, selector


def test_controls_look_interactive_rather_than_decorative(css: str):
    """A pointer cursor and a transition are what make a button read as one."""

    block = css[css.index(".admin-main button,.admin-sidebar nav button{") :]
    block = block[: block.index("}")]
    assert "cursor:pointer" in block
    assert "transition:" in block


def test_focus_is_made_visible_and_never_removed(css: str):
    """Keyboard operation must survive the polish."""

    assert "outline:2px solid var(--accent2)" in css
    assert "outline-offset:2px" in css
    # Buttons, the details toggle, links and every form field are covered. The
    # shared form rule sets outline:none for the chat composer, so the Admin
    # console restores a real ring rather than relying on its 10%-alpha one.
    ring = css[css.index(".admin-main button:focus-visible") :]
    ring = ring[: ring.index("}")]
    for element in ("button", "summary", "select", "input", "textarea", "a"):
        assert f".admin-main {element}:focus-visible" in ring, element
    # And no rule in the Admin block strips a focus ring back off. Comments are
    # removed first, since they discuss the very declaration being ruled out.
    admin = re.sub(r"/\*.*?\*/", "", css[css.index("/* Admin session/notice banner") :],
                   flags=re.S).replace(" ", "")
    assert "outline:none" not in admin
    assert "outline:0" not in admin


def test_no_state_is_carried_by_colour_alone(js: str, css: str, html: str):
    """Every state also has a word or a glyph."""

    # Control states render their name, not just a hue.
    assert "button[data-control-label]:after" in css
    assert "content:\" \\00b7 \" attr(data-control-label)" in css
    assert "button.dataset.controlLabel = CONTROL_LABELS[state]" in js
    # Stage and result states carry glyphs.
    assert "const STAGE_MARKS" in js
    assert "const RESULT_MARKS" in js
    # The higher-risk card says so in words.
    assert "Higher risk" in html


def test_the_busy_state_is_distinct_from_the_disabled_state(css: str, js: str):
    """"Working" and "switched off" must not look the same."""

    assert '.admin-main button[data-busy="true"]:before' in css
    assert "admin-spin" in css
    assert "function markBusy" in js
    assert "function clearBusy" in js


def test_a_click_is_acknowledged_before_the_network_call(tools: str):
    """The pressed/loading state is set before the first request goes out."""

    body = tools[tools.index("async function runDesktop") :]
    body = body[: body.index("async function executeDesktop")]
    assert body.index("markBusy(options.origin, options.busyLabel)") < body.index(
        "await executeDesktop"
    )
    # And the pressed state itself is CSS, so it needs no round trip at all.
    assert ".admin-main button:active:not([disabled])" in ADMIN_CSS.read_text()


def test_the_busy_state_is_cleared_on_every_exit_path(tools: str):
    """Including the thrown-error path, which is where it would strand."""

    body = tools[tools.index("async function runDesktop") :]
    body = body[: body.index("async function executeDesktop")]
    finally_block = body[body.index("} finally {") :]
    assert "desktopBusy = false;" in finally_block
    assert "clearBusy();" in finally_block


def test_duplicate_submissions_are_refused_while_an_action_runs(tools: str):
    """Both at the entry point and on the controls themselves."""

    assert tools.count("if (desktopBusy || sessionDead) return;") >= 3
    # applyControlState disables every registered control while busy.
    assert "button.disabled = desktopBusy || sessionDead || !enabled;" in tools


# ====================================================== result presentation ===


def test_the_result_leads_with_a_sentence_not_a_json_dump(html: str, tools: str):
    assert 'id="desktop-result-summary"' in html
    assert "function summarizeAction" in tools
    assert "function showResult" in tools


def test_the_raw_structured_result_is_kept_behind_technical_details(html: str, tools: str):
    """Diagnostics are demoted, never hidden."""

    assert 'id="desktop-result-details"' in html
    assert "<summary>Technical details</summary>" in html
    assert 'id="desktop-result"' in html
    # The raw object still reaches that element, including while polling.
    assert 'document.querySelector("#desktop-result").textContent = pretty(raw)' in tools
    assert "output.textContent = pretty(job)" in tools


def test_application_launch_is_summarized_in_words_not_fields(tools: str):
    """`visible_window: true, session_id: 1` is not a sentence."""

    assert "in Windows session ${data.session_id}" in tools
    assert "${title} launched${session}" in tools
    # Already running is neither a failure nor a second launch.
    assert 'data.state === "already_running"' in tools
    assert "${title} already running${session}" in tools


def test_a_failed_job_is_not_summarized_as_a_success(tools: str):
    assert '["failed", "cancelled", "expired"].includes(result.state)' in tools
    assert "result.failure_reason || result.failure_code" in tools
    # And the caller that observes afterwards is told it did not run.
    assert "succeeded = summary.ok !== false;" in tools


def test_job_progress_comes_from_the_coordinator_not_a_timer(tools: str):
    assert "function describeJob" in tools
    for field in ("job.state", "job.stage", "job.progress"):
        assert field in tools
    # No invented countdown or fake percentage.
    assert "Math.round(job.progress * 100)" in tools


def test_the_job_poll_issues_one_request_per_iteration(tools: str):
    """The previous form made a second identical request per poll."""

    loop = tools[tools.index("for (let poll = 0;") :]
    loop = loop[: loop.index("result = job;")]
    assert loop.count('api("/api/actions/jobs/"') == 1


# ================================================= truthfulness regressions ===


def test_the_five_desktop_axes_are_still_rendered_separately(tools: str, css: str):
    """Scannability must not become a single ONLINE badge."""

    assert "AXIS_LABELS" in tools
    for axis in ("power", "network", "os", "session", "agent"):
        assert axis in tools
    # Laid out as a grid of chips -- a presentation change, not a merge.
    assert ".axis-grid" in css
    assert ".axis[data-tone=" in css
    for collapsed in ('textContent = "ONLINE"', 'textContent = "OFFLINE"'):
        assert collapsed not in tools


def test_the_four_state_control_model_survived(tools: str):
    for state in ("available", "unavailable", "not_configured", "requires_authorization"):
        assert state in tools
    assert "function privilegedState" in tools
    assert 'elevated ? ["available", availableReason]' in tools


def test_wake_timing_out_is_not_reported_as_a_wake_failure(tools: str):
    """WOL succeeding and the desktop being slow are different things."""

    assert "Wake sent. Desktop is still starting or has not yet become reachable." in tools
    tail = tools[tools.index("async function observeWakeProgress") :]
    expiry = tail[tail.index("Wake sent. Desktop is still starting") :]
    # Reported as a true outcome, not a red cross.
    assert "showResult({ok: false" not in expiry[: expiry.index("}\n")]


def test_registered_applications_stay_represented_while_the_agent_is_gone(tools: str):
    """Controls must not appear to materialise out of nowhere on reconnect."""

    assert "let knownApps = []" in tools
    assert "renderAppCards(area, knownApps, agent, true)" in tools
    assert "waiting for Desktop Agent" in tools
    # And the reason is shown, not merely implied by the control being missing.
    assert "Launching resumes when it reconnects" in tools


def test_application_state_is_not_shown_stale_while_the_agent_is_gone(tools: str):
    """A remembered registry must not claim a remembered `running`."""

    cards = tools[tools.index("function renderAppCards") :]
    cards = cards[: cards.index("async function renderVms")]
    condition = cards[cards.index("const condition = remembered") :]
    condition = condition[: condition.index(";")]
    assert 'remembered ? "waiting for Desktop Agent"' in condition


def test_no_browser_terminal_or_command_interface_was_added(js: str, html: str):
    """Scope guard: launching Git Bash is not an SSH console."""

    for forbidden in ("xterm", "PTY", "pty", "WebSocket(\"/api/desktop/shell",
                      "desktop.exec", "desktop.shell", "arbitrary_command",
                      "terminal"):
        assert forbidden not in js
        assert forbidden not in html


# ============================================================ accessibility ===


def test_state_changes_are_announced(html: str, tools: str):
    for region in ("desktop-summary", "desktop-agent-status", "desktop-result-summary",
                   "desktop-wake-progress", "desktop-shutdown-progress"):
        pattern = rf'id="{region}"[^>]*role="status"'
        assert re.search(pattern, html), region
    assert 'button.setAttribute("aria-busy", "true")' in tools
    assert 'button.setAttribute("aria-disabled", String(button.disabled))' in tools


def test_stage_state_is_available_to_a_screen_reader(tools: str, css: str):
    """The glyph is aria-hidden, so the state is spelled out beside it."""

    assert "const STAGE_WORDS" in tools
    assert 'mark.setAttribute("aria-hidden", "true")' in tools
    assert 'spoken.className = "visually-hidden"' in tools
    assert ".visually-hidden{" in css


def test_the_confirmation_is_keyboard_operable(tools: str):
    assert "go.focus();" in tools
    assert 'event.key === "Escape"' in tools
    # Focus returns to the control that opened it.
    assert 'document.querySelector("#desktop-shutdown").focus();' in tools


def test_reduced_motion_is_respected(css: str):
    block = css[css.index("@media(prefers-reduced-motion:reduce){") :]
    assert "transition:none!important" in block
    assert "transform:none" in block
    # The spinner stops but the busy state stays visible.
    assert 'button[data-busy="true"]:before{animation:none' in block


# =================================================================== mobile ===


def test_touch_targets_are_large_enough(css: str):
    block = css[css.index(".admin-main button,.admin-sidebar nav button{") :]
    block = block[: block.index("}")]
    assert "min-height:44px" in block
    # The details toggle is a target too.
    assert ".tech-details summary{" in css
    summary = css[css.index(".tech-details summary{") :]
    assert "min-height:44px" in summary[: summary.index("}")]


def test_narrow_widths_get_a_usable_layout(css: str):
    block = css[css.index("@media(max-width:600px){") :]
    block = block[: block.index("\n}")]
    # Buttons stack and fill, rather than crowding into a scrolling row.
    assert ".admin-main .button-row{display:grid" in block
    assert "width:100%" in block
    assert ".app-grid{grid-template-columns:1fr}" in block


def test_wide_content_never_scrolls_the_page_sideways(css: str):
    """Long axis values and app names wrap instead of pushing the layout."""

    for rule in ("word-break:break-word", "flex-wrap:wrap", "min-width:0"):
        assert rule in css


# ================================================================ integrity ===


def test_every_styled_data_attribute_is_actually_set_by_the_script(js: str, css: str):
    """A CSS state that nothing sets is a state the user never sees."""

    for attribute, setter in (
        ("data-control-state", "dataset.controlState"),
        ("data-control-label", "dataset.controlLabel"),
        ("data-busy", 'dataset.busy = "true"'),
        ("data-outcome", "dataset.outcome"),
        ("data-stage", "dataset.stage"),
        ("data-tone", "dataset.tone"),
        ("data-condition", "dataset.condition"),
    ):
        assert attribute in css, attribute
        assert setter in js, setter


def test_every_class_the_script_creates_is_styled(js: str, css: str):
    """Catches a renamed class that silently loses all of its styling."""

    created = set(re.findall(r'className = "([a-z][a-z0-9 -]*)"', js))
    for value in sorted(created):
        for name in value.split():
            if name in {"visually-hidden", "primary-button", "secondary-button",
                        "danger-button", "code-output", "warning-card", "data-row",
                        "metric-card", "trace-card", "button-row"}:
                continue
            assert f".{name}" in css, name


def test_admin_js_parses_and_reaches_only_elements_that_exist(js: str, html: str):
    esprima = pytest.importorskip("esprima")
    esprima.parseScript(js, options={"tolerant": False})
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'querySelector\("#([A-Za-z0-9_-]+)"\)', js))
    used |= set(re.findall(r'querySelectorAll\("#([A-Za-z0-9_-]+)', js))
    created = set(re.findall(r'\.id = "([^"]+)"', js))
    assert used
    assert not used - ids - created
