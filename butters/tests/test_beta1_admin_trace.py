"""Static contract coverage for live-trace selection without a browser runtime."""

from __future__ import annotations

from pathlib import Path

ADMIN_JS = (
    Path(__file__).resolve().parents[1]
    / "src/butters/web/static/assets/admin.js"
).read_text(encoding="utf-8")


def _function(name: str) -> str:
    marker = f"function {name}("
    start = ADMIN_JS.index(marker)
    brace = ADMIN_JS.index("{", start)
    depth = 0
    for index in range(brace, len(ADMIN_JS)):
        if ADMIN_JS[index] == "{":
            depth += 1
        elif ADMIN_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return ADMIN_JS[brace : index + 1]
    raise AssertionError(f"unbalanced function {name}")


def test_trace_selection_has_stable_application_state_and_identity() -> None:
    create = _function("createTraceCard")
    select = _function("selectTrace")

    assert "let selectedTraceId = null" in ADMIN_JS
    assert "const traceCards = new Map()" in ADMIN_JS
    assert "card.dataset.traceId=traceId" in create
    assert "traceCards.set(traceId,card)" in create
    assert "selectedTraceId=previous===traceId?null:traceId" in select
    assert "traceCards.get(previous).open=false" in select
    assert "traceCards.get(selectedTraceId).open=true" in select


def test_incoming_trace_update_reuses_cards_and_keeps_selected_detail() -> None:
    render = _function("renderTraces")
    update = _function("updateTraceCard")

    assert "traceCards.get(trace.trace_id)||createTraceCard(trace.trace_id)" in render
    assert "updateTraceCard(card,trace)" in render
    assert "container.append(card)" in render
    assert "container.replaceChildren()" not in render
    assert "traceId!==selectedTraceId" in render
    assert "card.open=selectedTraceId===trace.trace_id" in update
    # Events may mutate, so only the child event rows are replaced. The keyed
    # details node—and therefore the user's selection—survives live polling.
    assert "events.replaceChildren()" in update


def test_selected_trace_can_update_close_and_switch_predictably() -> None:
    update = _function("updateTraceCard")

    assert 'close.textContent="Close detail"' in update
    assert "event.stopPropagation()" in update
    assert "selectTrace(trace.trace_id)" in update
    assert "previous&&traceCards.has(previous)" in _function("selectTrace")
    assert (
        'document.addEventListener("click",event=>{if(selectedTraceId&&!event.target.closest(".trace-card"))selectTrace(selectedTraceId);});'
        in ADMIN_JS
    )


def test_live_stream_continues_while_detail_is_open() -> None:
    connect = _function("connectTraceSocket")

    assert 'data.type==="traces"' in connect
    assert "renderTraces(data.traces)" in connect
    assert "selectedTraceId" not in connect
    assert "traceSocket.close" not in _function("selectTrace")
