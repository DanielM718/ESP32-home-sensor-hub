"""Admin page contract for KR260 JTAG recovery (static: no browser here)."""

from __future__ import annotations

import re
from pathlib import Path

from butters.integrations import kr260_jtag

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
HTML = (STATIC / "admin.html").read_text()
JS = (STATIC / "assets/admin.js").read_text()


def _object_keys(name: str) -> set[str]:
    body = re.search(rf"const {name} = \{{(.*?)\n\}};", JS, re.DOTALL)
    assert body, name
    # Drop string literals, then every remaining `name:` is a key.
    bare = re.sub(r'"(?:[^"\\]|\\.)*"', '""', body[1])
    return set(re.findall(r"([a-z_]+):", bare))


def _function(name: str) -> str:
    match = re.search(rf"(?:async )?function {name}\(.*?\n\}}\n", JS, re.DOTALL)
    assert match, name
    return match[0]


def test_jtag_recovery_is_a_separate_card_from_the_kexec_restart():
    kexec = HTML.index('id="kr260-card"')
    jtag = HTML.index('id="kr260-jtag-card"')
    assert kexec < HTML.index('id="kr260-reboot"') < jtag
    assert HTML.index('id="kr260-jtag-reset"') > jtag
    assert "Restart KR260 (kexec)" in HTML


def test_jtag_buttons_start_disabled_and_say_what_they_do():
    for button in ("kr260-jtag-check", "kr260-jtag-reset"):
        tag = re.search(rf'<button id="{button}"[^>]*>', HTML)[0]
        assert "disabled" in tag
    assert "JTAG power-on reset (abrupt)" in HTML
    assert "Abrupt hardware reset" in HTML
    assert "never retried automatically" in HTML


def test_every_state_butters_reports_has_text():
    assert _object_keys("KR260_JTAG_BOARD") >= kr260_jtag.BOARD_STATES
    assert _object_keys("KR260_JTAG_ATTEMPT") >= kr260_jtag.ATTEMPT_STATES
    assert _object_keys("KR260_JTAG_EVIDENCE") >= kr260_jtag.EVIDENCE
    assert _object_keys("KR260_JTAG_REASON") >= kr260_jtag.BLOCKED_REASONS


def test_the_page_posts_only_fixed_operations_to_the_fixed_route():
    posts = re.findall(r'api\("(/api/admin/tools/kr260[^"]*)"', JS)
    assert set(posts) == {
        "/api/admin/tools/kr260?refresh=1",
        "/api/admin/tools/kr260/reboot",
        "/api/admin/tools/kr260/jtag",
    }
    run = _function("runKr260Jtag")
    assert "JSON.stringify({operation,confirm:true})" in run
    listeners = re.findall(r'confirmKr260Jtag\("([a-z]+)"\)', JS)
    assert set(listeners) == {"check", "reset"}


def test_no_console_or_free_text_reaches_the_board():
    card = HTML[HTML.index('id="kr260-jtag-card"') :]
    card = card[: card.index("</section>")]
    assert "<input" not in card and "<textarea" not in card
    for word in ("xsdb", "tcl", "command", "shell"):
        assert word not in _function("runKr260Jtag").lower()
    assert "rst -system" not in JS


def test_one_request_per_confirmation_and_no_retry():
    run = _function("runKr260Jtag")
    assert run.index("if(kr260JtagInFlight) return;") < run.index("api(")
    assert "kr260JtagInFlight=true;" in run
    assert run.count('api("/api/admin/tools/kr260/jtag"') == 1
    # No loop, no recursion, no timer that could send again.
    assert "runKr260Jtag(" not in run[run.index("{") :]
    assert not re.search(r"\b(for|while)\s*\(", run)
    assert "setTimeout" not in run and "setInterval" not in run
    assert "It will not be retried automatically." in run
    # The confirm button fires once and needs the acknowledgement box.
    confirm = _function("confirmKr260Jtag")
    assert "{once:true}" in confirm
    assert "yes.disabled=true" in confirm and "yes.disabled=!box.checked" in confirm


def test_the_page_requires_a_fresh_passkey_bound_to_the_action():
    run = _function("runKr260Jtag")
    assert 'result.status==="authentication_required"' in run
    assert (
        'authenticatePurpose("pending_action",null,result.pending_action.pending_action_id)'
        in run
    )


def test_success_text_never_claims_the_board_restarted():
    run = _function("runKr260Jtag")
    reset_text = re.search(r'"Reset issued, not confirmed\.[^"]*"', run)
    assert reset_text
    assert "new boot ID" in reset_text[0]
    assert "recovered" not in run.lower() and "restarted" not in run.lower()


def test_buttons_follow_the_server_decision_only():
    render = _function("renderKr260Jtag")
    assert "check.disabled=kr260JtagInFlight||!recovery.check_permitted;" in render
    assert "reset.disabled=kr260JtagInFlight||!recovery.reset_permitted;" in render
