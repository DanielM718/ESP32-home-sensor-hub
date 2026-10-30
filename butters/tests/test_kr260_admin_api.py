"""Administrator KR260 endpoints: authorization, confirmation, closed gates."""

from __future__ import annotations

import asyncio

import httpx
from test_nas_admin_controls import _application, _elevate, _session

ADMIN = {"tailscale-user-login": "admin@example.com"}


async def _exercise(tmp_path) -> dict[str, object]:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    observed: dict[str, object] = {}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anonymous_http:
            anonymous = await _session(anonymous_http, {})
            observed["anonymous_status"] = (
                await anonymous_http.get("/api/admin/tools/kr260", headers=anonymous)
            ).status_code
            observed["anonymous_reboot"] = (
                await anonymous_http.post(
                    "/api/admin/tools/kr260/reboot",
                    headers=anonymous,
                    json={"confirm": True},
                )
            ).status_code

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            observed["status"] = (
                await http.get("/api/admin/tools/kr260?refresh=1", headers=ADMIN)
            ).json()
            mutation = await _session(http, ADMIN)
            await _elevate(http, mutation)
            for name, payload in (
                ("unconfirmed", {"confirm": False}),
                ("smuggled", {"confirm": True, "host": "192.168.1.9"}),
                ("command", {"confirm": True, "command": "poweroff"}),
                ("empty", {}),
                ("gated", {"confirm": True}),
            ):
                response = await http.post(
                    "/api/admin/tools/kr260/reboot", headers=mutation, json=payload
                )
                observed[name] = (response.status_code, response.json())
            observed["shutdown"] = (
                await http.post(
                    "/api/admin/tools/kr260/shutdown",
                    headers=mutation,
                    json={"confirm": True},
                )
            ).status_code
    finally:
        await app.state.shutdown_workers()
        del service
    return observed


def test_kr260_admin_endpoints_are_authorized_confirmed_and_gated(tmp_path) -> None:
    observed = asyncio.run(_exercise(tmp_path))

    assert observed["anonymous_status"] in {401, 403}
    assert observed["anonymous_reboot"] in {401, 403}

    status = observed["status"]
    assert status["online"] is False
    assert status["error"] == "disabled"
    assert status["capability"] == {
        "monitoring": False,
        "reboot": False,
        "shutdown": False,
        "jtag_reset": False,
    }

    for name in ("unconfirmed", "smuggled", "command", "empty"):
        code, body = observed[name]
        assert code in {400, 403, 409}, (name, body)
    assert observed["unconfirmed"][1]["error"] == "confirmation_required"
    assert observed["smuggled"][1]["error"] == "invalid_request"
    # The shipped gate is closed: nothing is queued.
    code, body = observed["gated"]
    assert (code, body["error"]) == (409, "capability_unavailable")
    assert "jobs" not in body
    # No shutdown capability exists at all.
    assert observed["shutdown"] in {404, 405}


async def _exercise_jtag(tmp_path) -> dict[str, object]:
    app, service, _nas = _application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    observed: dict[str, object] = {}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as anonymous_http:
            anonymous = await _session(anonymous_http, {})
            observed["anonymous"] = (
                await anonymous_http.post(
                    "/api/admin/tools/kr260/jtag",
                    headers=anonymous,
                    json={"operation": "reset", "confirm": True},
                )
            ).status_code

        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            mutation = await _session(http, ADMIN)
            await _elevate(http, mutation)
            for name, payload in (
                ("unconfirmed", {"operation": "reset", "confirm": False}),
                ("no_operation", {"confirm": True}),
                ("other_operation", {"operation": "system", "confirm": True}),
                ("rst_system", {"operation": "rst -system", "confirm": True}),
                (
                    "smuggled",
                    {"operation": "reset", "confirm": True, "serial": "XFL1EAUJ5SPOA"},
                ),
                ("gated_check", {"operation": "check", "confirm": True}),
                ("gated_reset", {"operation": "reset", "confirm": True}),
            ):
                response = await http.post(
                    "/api/admin/tools/kr260/jtag", headers=mutation, json=payload
                )
                observed[name] = (response.status_code, response.json())
    finally:
        await app.state.shutdown_workers()
        del service
    return observed


def test_kr260_jtag_endpoint_is_authorized_confirmed_and_gated(tmp_path) -> None:
    observed = asyncio.run(_exercise_jtag(tmp_path))

    assert observed["anonymous"] in {401, 403}
    for name in (
        "unconfirmed",
        "no_operation",
        "other_operation",
        "rst_system",
        "smuggled",
    ):
        code, body = observed[name]
        assert code in {400, 403, 409}, (name, body)
        assert "jobs" not in body
    assert observed["unconfirmed"][1]["error"] == "confirmation_required"
    # The shipped gate is closed: nothing is queued for either operation.
    for name in ("gated_check", "gated_reset"):
        code, body = observed[name]
        assert (code, body["error"]) == (409, "capability_unavailable"), (name, body)
        assert "jobs" not in body


# ----- an open gate: fresh passkey, one reset, no replay ---------------------------------


class _DarkObserver:
    """A board that has been dark; each probe gets a new timestamp."""

    def __init__(self, settings):
        self.settings = settings
        self.probes = 0

    def observe(self, *, refresh=False):
        self.probes += 1
        return {
            "device": "kr260",
            "operational_state": "offline",
            "online": False,
            "network_reachable": False,
            "ssh_available": False,
            "identity_verified": False,
            "observed_at": float(self.probes),
            "error": "host_unreachable",
        }

    def invalidate(self):
        pass


class _AgentHub:
    configured = True

    def __init__(self):
        self.calls = []

    def status(self):
        return {"state": "connected"}

    def supports(self, action):
        return True

    def kr260_jtag(self, action, *, cancel=None, idempotency_key=None):
        self.calls.append((action, idempotency_key))
        return {
            "action": action,
            "success": True,
            "jtag_result": "RST_ISSUED",
            "reset_may_have_run": True,
            "reset_issued_at": "2026-10-29T03:26:40.012Z",
        }


def _jtag_application(tmp_path):
    from dataclasses import replace

    import test_nas_admin_controls as nas_tests

    from butters.integrations import kr260_jtag

    base = nas_tests.load_assistant_settings()
    opened = replace(base.actions.kr260_jtag_recovery, enabled=True, configured=True)
    original = nas_tests.load_assistant_settings

    def settings_with_jtag():
        loaded = original()
        return replace(
            loaded,
            kr260=replace(
                loaded.kr260,
                enabled=True,
                host="192.168.1.125",
                expected_machine_id="7a6ccc6abe67407b93558d1efcb36587",
            ).validated(),
            actions=replace(loaded.actions, kr260_jtag_recovery=opened),
        )

    nas_tests.load_assistant_settings = settings_with_jtag
    try:
        app, service, _nas = _application(tmp_path)
    finally:
        nas_tests.load_assistant_settings = original
    adapter = service.assistant.kr260_adapter
    adapter.observer = _DarkObserver(adapter.observer.settings)
    hub = _AgentHub()
    persisted = adapter._persisted_reset_at
    adapter.attach_jtag(hub, persisted_reset_at=persisted)
    # Dark long enough: two probes, DARK_SECONDS apart.
    adapter._dark.since = adapter.monotonic() - kr260_jtag.DARK_SECONDS - 1
    adapter._dark.count = 2
    return app, service, hub


def test_the_service_counts_a_stored_kexec_restart_job(tmp_path) -> None:
    # The web service wires the job store into the quiet period after a kexec
    # restart, exactly as it does for the reset interval.
    _app, service, hub = _jtag_application(tmp_path)
    adapter = service.assistant.kr260_adapter
    assert adapter._persisted_kexec_at() is None
    store = service.action_state
    with store._lock, store._connect() as connection:
        connection.execute(
            "INSERT INTO action_jobs (job_id,plan_id,skill,summary,session_id,identity,"
            "created_at,state,stage,failure_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "j1",
                "p",
                "reboot_kr260",
                "s",
                "s",
                "i",
                store.clock(),
                "completed",
                "completed",
                None,
            ),
        )
    assert adapter._persisted_kexec_at() is not None
    assert adapter.status()["jtag_recovery"]["reset_blocked_reason"] == "restart_recent"
    assert hub.calls == []


async def _passkey(http, mutation, plan):
    from test_nas_admin_controls import _credential

    begin = await http.post(
        "/api/auth/authenticate/options",
        headers=mutation,
        json={"purpose": "pending_action", "pending_action_id": plan},
    )
    if begin.status_code != 200:
        # A consumed or expired plan is refused before any ceremony starts.
        return begin
    return await http.post(
        "/api/auth/authenticate/verify",
        headers=mutation,
        json={"ceremony_id": begin.json()["ceremony_id"], "credential": _credential()},
    )


async def _exercise_open_gate(tmp_path) -> dict[str, object]:
    from test_nas_admin_controls import _await_job

    app, service, hub = _jtag_application(tmp_path)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    observed: dict[str, object] = {"hub": hub}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as http:
            before = (
                await http.get("/api/admin/tools/kr260?refresh=1", headers=ADMIN)
            ).json()
            observed["before"] = before["jtag_recovery"]
            mutation = await _session(http, ADMIN)
            # Even an elevated session must present a fresh passkey for this plan.
            await _elevate(http, mutation)
            first = await http.post(
                "/api/admin/tools/kr260/jtag",
                headers=mutation,
                json={"operation": "reset", "confirm": True},
            )
            observed["first"] = first.json()
            observed["calls_before_passkey"] = len(hub.calls)
            plan = first.json()["pending_action"]["pending_action_id"]
            verified = await _passkey(http, mutation, plan)
            observed["job"] = await _await_job(
                http, mutation, verified.json()["jobs"][0]["job_id"]
            )
            # The same authorization cannot be used twice.
            replayed = await _passkey(http, mutation, plan)
            observed["replayed"] = replayed.status_code
            # A new authorization is refused by the 30-minute interval.
            second = await http.post(
                "/api/admin/tools/kr260/jtag",
                headers=mutation,
                json={"operation": "reset", "confirm": True},
            )
            again = await _passkey(
                http, mutation, second.json()["pending_action"]["pending_action_id"]
            )
            observed["second_job"] = await _await_job(
                http, mutation, again.json()["jobs"][0]["job_id"]
            )
            after = (
                await http.get("/api/admin/tools/kr260?refresh=1", headers=ADMIN)
            ).json()
            observed["after"] = after["jtag_recovery"]
            observed["audit"] = [
                entry
                for entry in service.action_state.audit_entries(200)
                if entry["skill"] == "reset_kr260_jtag"
            ]
    finally:
        await app.state.shutdown_workers()
        del service
    return observed


def test_open_gate_needs_a_fresh_passkey_and_sends_exactly_one_reset(tmp_path) -> None:
    observed = asyncio.run(_exercise_open_gate(tmp_path))
    hub = observed["hub"]

    assert observed["before"]["reset_permitted"] is True
    assert observed["before"]["board"] == "unreachable_confirmed"

    first = observed["first"]
    assert first["status"] == "authentication_required"
    assert first["authentication_required"] == "fresh"
    assert observed["calls_before_passkey"] == 0

    job = observed["job"]
    assert job["state"] == "completed", job
    assert len(hub.calls) == 1
    action, key = hub.calls[0]
    assert action == "kr260.jtag_por.reset"
    assert key is not None  # the job's stable idempotency key

    assert observed["replayed"] in {400, 403, 404, 409}
    assert observed["second_job"]["state"] == "failed"
    assert observed["second_job"]["failure_code"] == "reset_rate_limited"
    assert len(hub.calls) == 1

    after = observed["after"]
    assert after["reset_permitted"] is False
    assert after["reset_blocked_reason"] == "reset_rate_limited"
    assert after["last_attempt"]["state"] == "requested"
    assert after["last_attempt"]["jtag_result"] == "RST_ISSUED"

    # Every step is audited: two pending authorizations, then each outcome.
    audit = observed["audit"]
    outcomes = [entry["outcome"] for entry in audit]
    assert outcomes.count("pending_auth") == 2
    assert all(entry["authentication"] == "fresh" for entry in audit)
    assert all(entry["arguments"] == {} for entry in audit)
    assert len(audit) > 2
