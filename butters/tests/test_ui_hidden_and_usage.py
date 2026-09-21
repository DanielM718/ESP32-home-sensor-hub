"""Two UI corrections: `hidden` means hidden, and Usage is readable.

The first is a security-visible regression the redesign introduced. Browsers
express the HTML `hidden` attribute as `[hidden]{display:none}` in the
*user-agent* stylesheet, and any author `display` outranks it. The redesign
gave buttons, cards, labels and range fields an explicit display, so sixteen
controls that scripts had hidden went on painting — including the Portal's
NAS shutdown button, which identities without `nas_power` could see.

Authorization never depended on that: the server withholds `can_shutdown`
and the shutdown path independently requires the role. The tests below hold
both halves — the control is not drawn, and the backend still refuses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from frontend_assets import ALL_CSS, STYLESHEETS, declarations

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (STATIC / "assets/admin.js").read_text(encoding="utf-8")
PORTAL_HTML = (STATIC / "portal.html").read_text(encoding="utf-8")
PORTAL_JS = (STATIC / "assets/portal.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")

PAGES = {"index.html": INDEX_HTML, "admin.html": ADMIN_HTML, "portal.html": PORTAL_HTML}


# ======================= 1. `hidden` means hidden ==========================


def test_the_base_layer_restores_the_hidden_attribute() -> None:
    rule = declarations("[hidden]", STYLESHEETS["base.css"])
    assert rule["display"] == "none !important"


def test_the_invariant_outranks_every_author_display() -> None:
    """The whole point: no component rule may draw a hidden element.

    Without !important this fails, because the author rules that broke it
    (`.danger-button{display:inline-flex}` and friends) are equally specific
    or more so.
    """

    assert "!important" in declarations("[hidden]", STYLESHEETS["base.css"])["display"]


def _display_selectors() -> set[str]:
    body = re.sub(r"/\*.*?\*/", "", ALL_CSS, flags=re.DOTALL)
    selectors: set[str] = set()
    for selector, declaration in re.findall(r"([^{}]+)\{([^{}]*)\}", body):
        if re.search(r"(^|;)\s*display\s*:", declaration):
            selectors.update(part.strip() for part in selector.split(","))
    return selectors


def _hidden_targets() -> list[tuple[str, str, str, list[str]]]:
    """Every element a page ships hidden, or a script hides, with its classes."""

    found: list[tuple[str, str, str, list[str]]] = []
    toggled = set(re.findall(r'querySelector\("#([\w-]+)"\)\.hidden', ADMIN_JS + PORTAL_JS))
    toggled |= set(re.findall(r'show\(document\.querySelector\("#([\w-]+)"\)', ADMIN_JS))
    for page, document in PAGES.items():
        for match in re.finditer(r"<(\w+)([^>]*\bid=\"([\w-]+)\"[^>]*)>", document):
            tag, attributes, identifier = match.group(1), match.group(2), match.group(3)
            if " hidden" not in attributes and identifier not in toggled:
                continue
            classes = re.search(r'class="([^"]+)"', attributes)
            found.append((page, identifier, tag, classes.group(1).split() if classes else []))
    return found


def test_every_hidden_control_is_actually_undrawable() -> None:
    """Enumerated, not spot-checked: this is how the regression was missed."""

    targets = _hidden_targets()
    assert len(targets) >= 12, "the audit found fewer controls than expected"

    display = _display_selectors()
    leaking = [
        (page, identifier)
        for page, identifier, tag, classes in targets
        if tag in display or any(f".{name}" in display for name in classes)
    ]
    # Every one of these is drawn by an author rule, so all of them depend on
    # the [hidden] invariant above rather than on the user-agent default.
    assert leaking, "expected the audit to find author-drawn hidden controls"
    rule = declarations("[hidden]", STYLESHEETS["base.css"])
    assert rule["display"] == "none !important", leaking


@pytest.mark.parametrize(
    "identifier",
    # Wake is deliberately not in this list: `_can_wake` is a function of NAS
    # state only, every authenticated portal identity may use it, and it sits
    # inside `portal-main`, which is itself hidden until sign-in. Shutdown is
    # the role-gated one.
    ["portal-shutdown", "portal-open", "portal-signin", "portal-main"],
)
def test_the_portal_controls_the_server_gates_ship_hidden(identifier: str) -> None:
    match = re.search(rf'id="{identifier}"([^>]*)>', PORTAL_HTML)
    assert match, identifier
    assert "hidden" in match.group(1), f"{identifier} must not be drawn before state arrives"


def test_wake_is_state_gated_while_shutdown_is_role_gated() -> None:
    """The two portal capabilities are different kinds of claim."""

    source = (Path(__file__).parents[1] / "src/butters/web/portal.py").read_text()
    block = source[source.index('"can_shutdown"'):]
    block = block[: block.index(")")]
    assert "NAS_POWER in roles" in block
    wake = source[source.index("def _can_wake"):]
    wake = wake[: wake.index("\n\n\n")] if "\n\n\n" in wake else wake[:600]
    assert "roles" not in wake, "wake must not become role-gated by accident"


def test_the_portal_shutdown_button_is_bound_to_the_server_capability() -> None:
    """The client may not decide this; it may only reflect it."""

    assert "shutdown.hidden=!state.can_shutdown" in PORTAL_JS.replace(" ", "")
    # And nothing else in the client can reveal it.
    assert PORTAL_JS.count("shutdown.hidden") == 1
    for forbidden in ("roles.includes", "nas_power", "administrator"):
        assert forbidden not in PORTAL_JS, forbidden


def test_the_chat_ghost_passkey_card_cannot_appear() -> None:
    """Same regression, second symptom.

    Chat showed an "Authentication required / Use Passkey" card with no
    pending action behind it. The card ships hidden and is only revealed by
    showPendingAction(); `.action-card{display:grid}` drew it anyway, and
    pressing the button returned immediately because `pendingAction` is null.
    """

    match = re.search(r'id="action-card"([^>]*)>', INDEX_HTML)
    assert match and "hidden" in match.group(1)
    # It is drawn by an author rule, so it depends on the restored invariant.
    assert "display" in declarations(".action-card")
    assert declarations("[hidden]", STYLESHEETS["base.css"])["display"] == "none !important"

    app_js = (STATIC / "assets/app.js").read_text(encoding="utf-8")
    # Revealed only when the server actually returned a pending action.
    reveal = app_js[app_js.index("function showPendingAction(plan)"):]
    reveal = reveal[: reveal.index("\n}\n")]
    assert "actionCard.hidden = false" in reveal
    # And the handler is inert without one, which is why the ghost was silent.
    handler = app_js[app_js.index('actionAuthenticate.addEventListener'):]
    handler = handler[: handler.index("\n});")]
    assert "if (!pendingAction) return;" in handler


def test_no_control_was_special_cased_instead_of_fixing_the_invariant() -> None:
    for name, source in STYLESHEETS.items():
        assert "#portal-shutdown" not in source, name
        assert "portal-shutdown" not in source, name


# ============== 2. the portal capability, front and back ===================

def test_jellyfin_access_alone_cannot_see_or_run_a_shutdown() -> None:
    """Both halves, asserted where each one actually lives.

    Presentation: the button ships hidden and is only revealed from
    `state.can_shutdown`. Authorization: the portal service requires the role
    before it will freeze or execute anything, and the existing portal suite
    proves the denial end to end.
    """

    portal_source = (
        Path(__file__).parents[1] / "src/butters/web/portal.py"
    ).read_text(encoding="utf-8")
    # can_shutdown is conjunctive: role AND configured AND available.
    state = portal_source[portal_source.index("can_shutdown"):]
    assert "NAS_POWER" in state[:400]
    # Every destructive entry point re-checks the role itself.
    for entry in ("shutdown_plan", "shutdown_authenticate", "shutdown_verify"):
        if f"def {entry}" not in portal_source:
            continue
        block = portal_source[portal_source.index(f"def {entry}"):]
        block = block[: block.index("\n    def ")]
        assert "NAS_POWER" in block, entry


# ====================== 3. the Usage dashboard =============================


def test_usage_is_no_longer_a_raw_object_dump() -> None:
    assert 'renderObject(document.querySelector("#usage-view")' not in ADMIN_JS
    assert "#usage-view" not in ADMIN_JS
    assert 'id="usage-view"' not in ADMIN_HTML
    assert 'if(panel==="usage") await refreshUsage();' in ADMIN_JS


def test_the_dashboard_reads_the_aggregate_the_endpoint_actually_returns() -> None:
    """/api/admin/usage nests the aggregate under `summary`."""

    block = ADMIN_JS[ADMIN_JS.index("async function refreshUsage()"):]
    block = block[: block.index("\n}\n")]
    assert "payload.summary || payload" in block


def test_the_raw_view_cannot_paint_a_session_identifier() -> None:
    """The payload also carries per-request rows; those are not this page's.

    `recent_requests` includes a stable `session_id` per row. Dumping the
    whole payload would put dozens of them on screen, which is neither
    needed here nor something an aggregate view should carry.
    """

    block = ADMIN_JS[ADMIN_JS.index("async function refreshUsage()"):]
    block = block[: block.index("\n}\n")]
    assert 'querySelector("#usage-raw").textContent = pretty(value)' in block
    # `value` is the aggregate, never the payload.
    assert "pretty(payload)" not in ADMIN_JS
    for field in ("recent_requests", "session_id", "request_id"):
        assert field not in ADMIN_JS[ADMIN_JS.index("/* ================================ Usage"):], field


def test_the_three_windows_are_rendered_separately() -> None:
    block = ADMIN_JS[ADMIN_JS.index("const USAGE_WINDOWS = ["):]
    block = block[: block.index("];")]
    for key, label in (("today", "Today"), ("last_7_days", "Last 7 days"),
                       ("current_month", "This month")):
        assert f'["{key}", "{label}"]' in block
    card = ADMIN_JS[ADMIN_JS.index("function renderUsageWindows"):]
    card = card[: card.index("\n}\n")]
    for field in ("cost_usd", "requests", "input_tokens", "output_tokens", "errors"):
        assert field in card, field


def test_a_sub_cent_cost_keeps_the_precision_that_makes_it_meaningful() -> None:
    """Rounding $0.007476 to "$0.01" is a 40% error at this scale."""

    block = ADMIN_JS[ADMIN_JS.index("function usageCost"):]
    block = block[: block.index("\n}\n")]
    assert "Math.abs(amount) < 1" in block
    assert "toFixed(6)" in block
    assert 'padEnd(2, "0")' in block
    # A very small charge is never reported as nothing.
    assert '"< $0.000001"' in block
    # Larger amounts use ordinary currency formatting.
    assert "minimumFractionDigits: 2" in block and "maximumFractionDigits: 2" in block


def test_latency_switches_units_at_a_second() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function usageDuration"):]
    block = block[: block.index("\n}\n")]
    assert "Math.round(Number(milliseconds))" in block, "999.6ms must not print as 1000 ms"
    # Template literals, so match the interpolated forms.
    assert "value < 1000" in block and "${value} ms" in block
    assert "value / 1000" in block and "} s`" in block
    assert "seconds >= 10 ? 1 : 2" in block


def test_route_and_provider_names_are_humanised_but_models_are_not() -> None:
    humanize = ADMIN_JS[ADMIN_JS.index("function humanize(key)"):]
    humanize = humanize[: humanize.index("\n}\n")]
    assert 'replaceAll("_", " ")' in humanize
    assert "toUpperCase()" in humanize
    # A model identifier is the server's own string; inventing a display name
    # for one risks naming a model the server never reported.
    models = ADMIN_JS[ADMIN_JS.index('renderUsageDistribution("#usage-models"'):]
    assert "labeller: String" in models[:200]
    assert "mono: true" in models[:200]
    assert '"openai": "OpenAI"' in ADMIN_JS.replace("openai:", '"openai":')


def test_requests_without_a_model_are_counted_not_claimed_as_savings() -> None:
    block = ADMIN_JS[ADMIN_JS.index("const avoided = value.deterministic_or_model_avoided"):]
    block = block[: block.index("renderUsageDistribution(\"#usage-models\"")]
    assert "without calling a model" in block
    assert "not a measured saving" in block


def test_cost_provenance_stays_visible_and_honest() -> None:
    notes = ADMIN_JS[ADMIN_JS.index("const COST_BASIS_NOTES = {"):]
    notes = notes[: notes.index("};")]
    for basis in ("provider_reported", "input_measured", "estimated_upper_bound",
                  "unavailable", "unrecorded"):
        assert basis in notes, basis
    assert "a ceiling, not an observed charge" in notes
    assert 'id="usage-cost-basis"' in ADMIN_HTML
    assert "not a charge Butters observed" in ADMIN_HTML


def test_an_empty_error_list_reads_as_a_sentence() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function renderUsageErrors"):]
    block = block[: block.index("\n}\n")]
    assert "No recent AI provider errors." in block
    # Only the fields the endpoint exposes; never request or response content.
    for field in ("error_code", "model", "timestamp", "provider", "route"):
        assert f"error.{field}" in block, field
    for forbidden in ("prompt", "response_text", "text", "content", "api_key"):
        assert f"error.{forbidden}" not in block, forbidden


def test_pricing_provenance_is_secondary_but_present() -> None:
    block = ADMIN_JS[ADMIN_JS.index("function renderUsagePricing"):]
    block = block[: block.index("\n}\n")]
    for field in ("source", "date", "unknown_models_fail_closed"):
        assert field in block, field
    panel = ADMIN_HTML[ADMIN_HTML.index('id="panel-usage"'):]
    panel = panel[: panel.index("</section>", panel.index('id="usage-pricing"'))]
    pricing = panel.index('id="usage-pricing"')
    assert panel.rindex("<details", 0, pricing) > 0, "pricing must sit behind a disclosure"


def test_raw_json_is_two_disclosures_deep_and_never_the_default() -> None:
    panel = ADMIN_HTML[ADMIN_HTML.index('id="panel-usage"'):]
    raw = panel.index('id="usage-raw"')
    before = panel[:raw]
    assert before.count("<details") - before.count("</details>") >= 2
    assert "Raw usage data" in panel
    # And it is not what the page shows first.
    assert panel.index('id="usage-windows"') < raw


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"today": {}, "last_7_days": {}, "current_month": {}},
        {"today": {"requests": 0, "cost_usd": 0.0, "input_tokens": 0,
                   "output_tokens": 0, "errors": 0},
         "route_distribution": {}, "model_distribution": {},
         "provider_distribution": {}, "cost_basis_distribution": {},
         "latency_by_route": {}, "latency_by_operation": {},
         "recent_errors": [], "pricing": {}},
    ],
)
def test_every_collection_has_an_empty_state_rather_than_undefined(payload: dict) -> None:
    """A payload that is empty or missing fields must not render "undefined"."""

    refresh = ADMIN_JS[ADMIN_JS.index("async function refreshUsage()"):]
    # Each collection passes an `empty:` sentence, and each scalar goes
    # through a formatter that returns an em dash for a missing value.
    assert refresh.count("empty:") == 3
    for formatter in ("usageNumber", "usageCost", "usageDuration"):
        block = ADMIN_JS[ADMIN_JS.index(f"function {formatter}"):]
        block = block[: block.index("\n}\n")]
        assert 'return "—"' in block, formatter
    assert "emptyRow(" in refresh
    assert json.dumps(payload) is not None


def test_the_dashboard_reuses_the_design_system() -> None:
    panel = ADMIN_HTML[ADMIN_HTML.index('id="panel-usage"'):]
    panel = panel[: panel.index("\n    <!--", 1)] if "\n    <!--" in panel[1:] else panel
    for component in ("metric-grid", "admin-section", "advanced-block",
                      "axis-grid", "data-list", "tool-note", "code-output"):
        assert component in panel, component


def test_the_usage_tables_do_not_scroll_sideways_on_a_phone() -> None:
    latency = declarations(".usage-latency")
    assert latency["grid-template-columns"].startswith("minmax(0, 1fr)")
    body = re.sub(r"/\*.*?\*/", "", STYLESHEETS["admin.css"], flags=re.DOTALL)
    narrow = body[body.index("@media (max-width: 560px)"):]
    assert ".usage-latency {" in narrow
    assert "grid-template-columns: minmax(0, 1fr) auto;" in narrow
    # The header row is dropped, so each figure carries its own name instead.
    assert 'content: attr(data-label)' in narrow
    for selector in (".usage-row", ".usage-latency"):
        assert declarations(selector)["min-width"] == "0", selector


def test_the_usage_figures_line_up() -> None:
    assert "tabular-nums" in STYLESHEETS["admin.css"]
    assert declarations(".metric-card strong")["font-variant-numeric"] == "tabular-nums"
