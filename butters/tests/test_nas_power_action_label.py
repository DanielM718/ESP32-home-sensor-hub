"""The Media Server card must name the power action that was actually requested.

The bug: after a successful protected shutdown, the Portal card read "Wake
packet sent 2 minutes ago". The backend was never confused -- it already keyed
`wake_elapsed_seconds` and the WAKE_SENT lifecycle on `operation == "wake_nas"`
specifically -- but `portal.js` rendered the string "Wake packet sent" for *any*
recent operation, discarding the `operation` field it was already being sent.

The tempting fix is to read the label off reachability, since a shut-down NAS
goes unreachable. That would be wrong in both directions: a NAS can be
unreachable because its switch lost power, and reachable because somebody
pressed its front panel. So the tests below pin the label to the requested
action and then vary the observations underneath it to prove the label does not
move.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import httpx
from butters.auth.store import JELLYFIN_ACCESS, NAS_POWER
from butters.web.portal import _POWER_ACTIONS, _portal_last_operation
from test_jellyfin_portal import _application, _enroll, _mutation, _sign_in

ASSETS = Path(__file__).resolve().parents[1] / "src/butters/web/static/assets"
STATIC = Path(__file__).resolve().parents[1] / "src/butters/web/static"

WAKE = {
    "subject": "nas",
    "operation": "wake_nas",
    "outcome": "wake_packet_sent",
    "detail": "portal:identity:someone@example.com",
    "at": time.time() - 120,
}
SHUTDOWN = {
    "subject": "nas",
    "operation": "nas.system.shutdown",
    "outcome": "shutdown_queued",
    "detail": "portal:nas_power",
    "at": time.time() - 120,
}


# --------------------------- 1, 2: the two actions ---------------------------


def test_a_wake_record_is_a_wake() -> None:
    projected = _portal_last_operation(WAKE)
    assert projected is not None
    assert projected["power_action"] == "wake"
    # The operation and outcome stay beside it; nothing was replaced.
    assert projected["operation"] == "wake_nas"
    assert projected["outcome"] == "wake_packet_sent"


def test_a_shutdown_record_is_a_shutdown() -> None:
    projected = _portal_last_operation(SHUTDOWN)
    assert projected is not None
    assert projected["power_action"] == "shutdown"
    assert projected["operation"] == "nas.system.shutdown"
    assert projected["outcome"] == "shutdown_queued"


# ------------------------ 3, 4: no cross-contamination -----------------------


def test_the_two_actions_never_map_to_each_other() -> None:
    assert _portal_last_operation(SHUTDOWN)["power_action"] != "wake"
    assert _portal_last_operation(WAKE)["power_action"] != "shutdown"
    # And the vocabulary is exactly these two, so a third cannot appear without
    # a deliberate change here.
    assert set(_POWER_ACTIONS.values()) == {"wake", "shutdown"}
    assert set(_POWER_ACTIONS) == {"wake_nas", "nas.system.shutdown"}


# ----------------------------- 5: nothing to say -----------------------------


def test_no_record_yields_no_projection() -> None:
    """Anything that is not a record at all projects to nothing."""

    for absent in (None, "", 0, [], ("wake_nas",)):
        assert _portal_last_operation(absent) is None

    # An empty dict *is* a record shape, so it projects -- but with no action,
    # which is what makes the line absent rather than mislabelled.
    empty = _portal_last_operation({})
    assert empty is not None and empty["power_action"] is None


def test_an_unrecognised_operation_yields_no_label() -> None:
    """Better a missing line than a confidently wrong one."""

    for operation in ("nas.system.reboot", "get_nas_status", "", None):
        projected = _portal_last_operation({**WAKE, "operation": operation})
        assert projected is not None
        assert projected["power_action"] is None


# ------------------- 6: the label ignores observed state ---------------------


def test_the_label_does_not_depend_on_reachability() -> None:
    """A record carries no observations, so the label cannot consult them.

    This is the structural half of the proof: `_portal_last_operation` receives
    only the operation record. Even a record that carries reachability-shaped
    keys cannot shift the answer.
    """

    for noise in (
        {"lan": "unreachable", "tailscale": "unreachable", "jellyfin": "unavailable"},
        {"lan": "reachable", "tailscale": "reachable", "jellyfin": "ready"},
        {"aggregate": "OFFLINE"},
        {"aggregate": "READY"},
        {"power_state": "off"},
    ):
        assert _portal_last_operation({**SHUTDOWN, **noise})["power_action"] == "shutdown"
        assert _portal_last_operation({**WAKE, **noise})["power_action"] == "wake"


def test_the_projection_reads_no_observation_source() -> None:
    """The other half: the function's own text names no observed-state field."""

    import inspect

    body = inspect.getsource(_portal_last_operation)
    executable = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    # Strip the docstring, which discusses reachability on purpose.
    executable = re.sub(r'""".*?"""', "", executable, flags=re.DOTALL)
    for forbidden in (
        "observations",
        "aggregate",
        "reachable",
        "unreachable",
        "jellyfin",
        "power_state",
        "lifecycle",
    ):
        assert forbidden not in executable, forbidden


# -------------------- 7: the timestamp still drives the age ------------------


def test_the_record_timestamp_still_drives_the_relative_time() -> None:
    now = time.time()
    for seconds in (0, 45, 120, 3600):
        projected = _portal_last_operation({**SHUTDOWN, "at": now - seconds})
        assert projected["at"] == now - seconds
        assert abs(projected["age_seconds"] - seconds) < 5

    # A clock that moved backwards must not produce a negative age.
    assert _portal_last_operation({**WAKE, "at": now + 600})["age_seconds"] == 0.0


# ------------------------- the rendered label itself -------------------------


def _portal_script() -> str:
    return (ASSETS / "portal.js").read_text()


def _dense(text: str) -> str:
    """Compare without depending on where the file happens to wrap."""

    return "".join(text.split())


def test_the_script_maps_each_action_to_its_own_wording() -> None:
    script = _dense(_portal_script())
    assert _dense('wake: "Wake packet sent"') in script
    assert _dense('shutdown: "Shutdown requested"') in script


def test_shutdown_is_not_described_as_a_packet() -> None:
    """The protected path is an agent request, not Wake-on-LAN."""

    script = _portal_script().lower()
    assert "shutdown packet" not in script
    assert "shutdown requested" in script


def test_the_label_is_no_longer_hard_coded_to_wake() -> None:
    """The defect was one unconditional string. It must not come back."""

    script = _portal_script()
    # The only place the wake wording appears is inside the action map.
    occurrences = script.count("Wake packet sent")
    assert occurrences == 1, occurrences
    assert _dense("POWER_ACTION_LABELS[state.last_operation.power_action]") in _dense(
        script
    )


def test_the_script_chooses_the_label_without_consulting_state() -> None:
    """The rendering line must not reach for observations or readiness."""

    script = _portal_script()
    line = next(
        item
        for item in script.splitlines()
        if "POWER_ACTION_LABELS[" in _dense(item)
    )
    for forbidden in ("observations", "aggregate", "jellyfin_ready", "can_wake"):
        assert forbidden not in line, forbidden


def test_the_line_is_absent_until_there_is_something_to_report() -> None:
    document = (STATIC / "portal.html").read_text()
    element = next(
        item
        for item in document.splitlines()
        if 'id="portal-last-operation"' in item
    )
    assert "hidden" in element
    # It starts empty rather than claiming nothing has been requested, because
    # the server may well report an action on the very first refresh.
    assert ">" in element and "Nothing has been requested" not in element

    assert _dense("last.hidden = !action") in _dense(_portal_script())

    # Hiding only works because the global rule beats the author `display`.
    base = (ASSETS / "base.css").read_text()
    assert "[hidden] { display: none !important; }" in " ".join(base.split())


# ------------- 8, 9, 10: everything around it stays where it was -------------


def test_observed_state_rendering_is_untouched() -> None:
    """The four observation rows and the readiness lines are unchanged."""

    script = _dense(_portal_script())
    for expected in (
        '["NAS (LAN)", state.observations.lan]',
        '["NAS OS / API", state.observations.nas_api]',
        '["Tailscale", state.observations.tailscale]',
        '["Jellyfin", state.observations.jellyfin]',
    ):
        assert _dense(expected) in script, expected
    assert "state.headline" in script
    assert "state.detail" in script
    assert _dense("renderBandwidth(state.bandwidth)") in script


def test_the_wake_and_shutdown_controls_still_follow_the_server() -> None:
    script = _dense(_portal_script())
    assert _dense("wake.hidden = !state.can_wake") in script
    assert _dense("shutdown.hidden = !state.can_shutdown") in script
    assert _dense("open.hidden = !state.jellyfin_ready") in script


def test_the_shutdown_authorization_path_is_unchanged() -> None:
    """This task is presentation only: the gates must read exactly as before."""

    import inspect

    from butters.web.portal import PortalService

    state = inspect.getsource(PortalService.nas_state)
    assert "NAS_POWER in roles" in state
    assert 'capability.get("shutdown_configured") is True' in state

    prepare = inspect.getsource(PortalService.prepare_shutdown)
    assert "self.require_role(session, NAS_POWER)" in prepare
    assert 'confirmed is not True' in prepare
    assert 'pending_confirmation=True' in prepare

    finish = inspect.getsource(PortalService.finish_shutdown_authentication)
    assert "self.require_role(session, NAS_POWER)" in finish
    assert "fresh_authentication_required" in finish

    wake = inspect.getsource(PortalService.wake)
    assert "self.require_role(session)" in wake
    assert "NAS_POWER" not in wake


# ----------------- the reported scenario, end to end over HTTP ---------------


async def _the_api_reports_the_action_the_observations_contradict(tmp_path) -> None:
    """The exact bug: shutdown succeeded, NAS went unreachable, card said Wake.

    The observations here are the ones from the report -- everything unreachable
    and Jellyfin unavailable -- and the stored action is a shutdown. If the label
    were inferred from reachability this is precisely where it would go wrong.
    """

    app, service, nas = _application(tmp_path, nas_agent_shutdown=True)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        _enroll(
            service,
            identity="owner@example.com",
            credential_id=b"owner-credential",
            roles=frozenset({JELLYFIN_ACCESS, NAS_POWER}),
        )
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            headers = await _mutation(http, "owner@example.com")
            await _sign_in(http, headers, credential_id=b"owner-credential")

            # FakeNas starts OFFLINE: lan/nas_api/tailscale unreachable,
            # Jellyfin unavailable -- the reported card exactly.
            service._record_operation(
                "nas", "nas.system.shutdown", "shutdown_queued", "portal:nas_power"
            )
            state = (await http.get("/api/portal/nas", headers=headers)).json()
            assert state["observations"]["lan"] == "unreachable"
            assert state["observations"]["jellyfin"] == "unavailable"
            assert state["last_operation"]["power_action"] == "shutdown"
            assert state["last_operation"]["power_action"] != "wake"

            # A shutdown must not light up the wake timer either.
            assert state["wake_elapsed_seconds"] is None

            # Now the mirror image: a wake while the NAS reads fully READY.
            nas.become_ready()
            service._record_operation(
                "nas", "wake_nas", "wake_packet_sent", "portal:owner"
            )
            ready = (await http.get("/api/portal/nas", headers=headers)).json()
            assert ready["observations"]["lan"] == "reachable"
            assert ready["observations"]["jellyfin"] == "ready"
            assert ready["last_operation"]["power_action"] == "wake"
            assert ready["wake_elapsed_seconds"] is not None

            # And with no action recorded at all there is nothing to label.
            service._last_operations.pop("nas", None)
            quiet = (await http.get("/api/portal/nas", headers=headers)).json()
            assert quiet["last_operation"] is None

        # Reporting a shutdown never performs one.
        assert nas.wakes == 0
    finally:
        await app.state.shutdown_workers()
        del service


def test_the_api_reports_the_action_the_observations_contradict(tmp_path) -> None:
    asyncio.run(_the_api_reports_the_action_the_observations_contradict(tmp_path))
