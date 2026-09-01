"""HTTP-layer security for the dashboard's state-changing routes.

The dashboard has no login by design: SECURITY.md places the trust boundary at
the LAN/Tailscale deployment. That assumption covers a trusted user calling the
API directly. It does not cover a page in that user's browser calling it for
them, so mutating routes check the origin explicitly rather than relying on the
JSON body parser to make cross-origin posts inconvenient.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.web import UNSAFE_METHODS, create_app
from test_printer import _app_settings, _PrinterRepository, _SensorRepository, _Status

DASHBOARD = "http://sensor-pi.tail9644cc.ts.net:8080"
HOSTILE = "http://evil.example"

MUTATIONS: tuple[tuple[str, str, Any], ...] = (
    ("POST", "/api/printer/maintenance/complete-all", {"confirm": True}),
    (
        "POST",
        "/api/printer/maintenance/x2d_z_axis_deep_maintenance/complete",
        {"confirm": True},
    ),
    ("POST", "/api/exports", {}),
    ("POST", "/api/monitoring/sessions", {}),
)


class _RecordingPrinter(_PrinterRepository):
    """Counts completions so a refused request can be shown to change nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.completions: list[tuple[str, str]] = []

    def complete_maintenance(self, task_id, *, notes, completed_at):
        self.completions.append((task_id, notes))
        return {"event_id": "e1", "task_id": task_id, "local_record_only": True}

    def complete_all_maintenance(self, *, notes, completed_at):
        self.completions.append(("*", notes))
        return {"completions": [], "local_record_only": True}


def _client(tmp_path, printer=None):
    app = create_app(
        _app_settings(tmp_path),
        repository=_SensorRepository(),
        printer_repository=printer or _RecordingPrinter(),
        status_provider=_Status(),
    )
    return app.test_client()


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_a_foreign_origin_cannot_change_state(
    tmp_path, method: str, path: str, body: Any
) -> None:
    printer = _RecordingPrinter()
    client = _client(tmp_path, printer)

    response = client.open(
        path, method=method, json=body, headers={"Origin": HOSTILE}
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "forbidden"
    assert printer.completions == []


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_the_dashboard_s_own_origin_is_accepted(
    tmp_path, method: str, path: str, body: Any
) -> None:
    client = _client(tmp_path)

    response = client.open(
        path,
        method=method,
        json=body,
        headers={"Origin": DASHBOARD, "Host": "sensor-pi.tail9644cc.ts.net:8080"},
        base_url=DASHBOARD,
    )

    assert response.status_code != 403
    assert "forbidden" not in response.get_data(as_text=True)


def test_the_dashboards_own_completion_actually_succeeds(tmp_path) -> None:
    """Guards that only ever refuse are indistinguishable from broken ones."""

    client = _client(tmp_path)

    response = client.post(
        "/api/printer/maintenance/complete-all",
        json={"confirm": True},
        headers={"Origin": DASHBOARD},
        base_url=DASHBOARD,
    )

    assert response.status_code == 201
    assert response.get_json()["local_record_only"] is True


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_a_client_that_sends_no_origin_still_works(
    tmp_path, method: str, path: str, body: Any
) -> None:
    """curl, the CLI and the verify scripts send no Origin header.

    The documented model permits them to reach the API directly, so an absent
    Origin must not be treated as a hostile one.
    """

    client = _client(tmp_path)

    response = client.open(path, method=method, json=body)

    assert response.status_code != 403


def test_reads_are_never_blocked_by_the_origin_check(tmp_path) -> None:
    client = _client(tmp_path)

    for path in ("/api/latest", "/api/status", "/api/printer", "/api/system-status"):
        response = client.get(path, headers={"Origin": HOSTILE})
        assert response.status_code == 200, path


def test_every_state_changing_method_is_covered(tmp_path) -> None:
    """A future route using PUT or PATCH must not slip past the guard."""

    assert UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}


def test_a_delete_from_a_foreign_origin_is_refused(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.delete(
        "/api/exports/2415a7d8-6ead-42d0-a6b3-30dcfb02b245",
        headers={"Origin": HOSTILE},
    )

    assert response.status_code == 403


# --- the completion route's own input handling ----------------------------


def test_completion_requires_an_explicit_confirmation(tmp_path) -> None:
    printer = _RecordingPrinter()
    client = _client(tmp_path, printer)

    for body in ({}, {"confirm": False}, {"confirm": "true"}, None):
        response = client.post(
            "/api/printer/maintenance/x2d_z_axis_deep_maintenance/complete",
            json=body,
        )
        assert response.status_code == 400, body
    assert printer.completions == []


def test_completion_rejects_a_non_string_note(tmp_path) -> None:
    printer = _RecordingPrinter()
    client = _client(tmp_path, printer)

    response = client.post(
        "/api/printer/maintenance/complete-all",
        json={"confirm": True, "notes": {"nested": "object"}},
    )

    assert response.status_code == 400
    assert printer.completions == []


def test_a_form_encoded_post_cannot_reach_the_completion_route(tmp_path) -> None:
    """The origin check is the guarantee; this pins the second line of defence."""

    printer = _RecordingPrinter()
    client = _client(tmp_path, printer)

    response = client.post(
        "/api/printer/maintenance/complete-all",
        data="confirm=true",
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert printer.completions == []
