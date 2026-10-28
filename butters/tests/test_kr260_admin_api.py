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
