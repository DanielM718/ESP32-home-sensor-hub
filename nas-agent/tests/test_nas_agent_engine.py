import threading

from butters_nas_agent.engine import Engine
from butters_nas_agent.protocol import ProtocolError


class Backend:
    def __init__(self, status=None, shutdown=None, error=None):
        self.value = status or {}
        self.shutdown_value = shutdown or {
            "accepted": True,
            "state": "scheduled",
            "method": "system.shutdown",
        }
        self.error = error
        self.shutdowns = 0

    def status(self):
        if self.error:
            raise ProtocolError(self.error)
        return dict(self.value)

    def shutdown(self):
        self.shutdowns += 1
        if self.error:
            raise ProtocolError(self.error)
        return dict(self.shutdown_value)


def _engine(system=None, jellyfin=None):
    local = Backend({"hostname": "agent", "uptime_seconds": 12.0})
    return Engine(
        local,
        system
        or Backend(
            {
                "reachable": True,
                "hostname": "nas",
                "version": "25.10",
                "uptime_seconds": 42,
                "system_state": "online",
            }
        ),
        jellyfin
        or Backend(
            {"reachable": True, "ready": True, "version": "10.10", "http_status": 200}
        ),
    )


def test_agent_status_is_small_and_has_connection_metadata():
    engine = _engine()
    engine.connected(2)
    engine.heartbeat_sent(4)
    value = engine.invoke("nas.agent.status", {})
    assert value["success"] is True
    assert value["connection_count"] == 2
    assert value["last_heartbeat_sequence"] == 4
    assert "secret" not in value


def test_system_and_jellyfin_status_are_independent():
    system = Backend(error="truenas_unavailable")
    engine = _engine(system=system)
    observed = engine.invoke("nas.system.status", {})
    assert observed["success"] is True
    assert observed["reachable"] is False
    assert observed["error"] == "truenas_unavailable"
    assert engine.invoke("nas.jellyfin.status", {})["ready"] is True
    heartbeat = engine.heartbeat_state()
    assert heartbeat["system"] == {"reachable": False, "error": "truenas_unavailable"}
    assert heartbeat["jellyfin"]["ready"] is True


def test_jellyfin_down_does_not_make_agent_or_system_unhealthy():
    engine = _engine(
        jellyfin=Backend(
            {"reachable": False, "ready": False, "version": None, "http_status": None}
        )
    )
    assert engine.invoke("nas.agent.status", {})["success"] is True
    assert engine.invoke("nas.system.status", {})["success"] is True
    assert engine.invoke("nas.jellyfin.status", {})["reachable"] is False


def test_shutdown_is_fixed_and_reports_backend_acceptance():
    backend = Backend()
    result = _engine(system=backend).invoke("nas.system.shutdown", {})
    assert result["success"] is True
    assert result["accepted"] is True
    assert backend.shutdowns == 1


def test_shutdown_refusal_and_timeout_are_enumerated():
    for code in ("operation_disabled", "truenas_refused", "timeout"):
        result = _engine(system=Backend(error=code)).invoke("nas.system.shutdown", {})
        assert result["success"] is False
        assert result["error"] == code


def test_arbitrary_shutdown_input_and_unknown_action_never_reach_backend():
    backend = Backend()
    engine = _engine(system=backend)
    for action, values in (
        ("nas.system.shutdown", {"delay": 3}),
        ("nas.system.shutdown", {"method": "system.reboot"}),
        ("nas.api.call", {}),
    ):
        assert engine.invoke(action, values)["success"] is False
    assert backend.shutdowns == 0


def test_cancelled_request_never_reaches_shutdown_backend():
    backend = Backend()
    cancel = threading.Event()
    cancel.set()
    assert (
        _engine(system=backend).invoke("nas.system.shutdown", {}, cancel)["error"]
        == "cancelled"
    )
    assert backend.shutdowns == 0
