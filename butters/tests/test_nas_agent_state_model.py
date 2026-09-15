"""Power truth must require independent evidence, never just an agent socket."""

from butters.web.service import BetaAssistantService


def _status(lan: str, api: str) -> dict[str, object]:
    return {"observations": {"lan": lan, "nas_api": api}}


def _agent(state: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "state": state,
        "system": None,
        "jellyfin": None,
        "shutdown_accepted_at": None,
    }
    value.update(changes)
    return value


def test_disconnect_alone_is_unknown_not_off() -> None:
    for observations in (
        _status("reachable", "unreachable"),
        _status("unreachable", "reachable"),
        _status("not_observed", "not_observed"),
    ):
        assert (
            BetaAssistantService._nas_power_state(observations, _agent("disconnected"))
            == "unknown"
        )


def test_off_requires_disconnect_lan_unreachable_and_api_unreachable() -> None:
    assert (
        BetaAssistantService._nas_power_state(
            _status("unreachable", "unreachable"), _agent("disconnected")
        )
        == "off"
    )
    assert (
        BetaAssistantService._nas_lifecycle(
            _status("unreachable", "unreachable"),
            _agent("disconnected"),
            None,
        )
        == "OFF"
    )


def test_agent_local_state_drives_online_and_shutting_down() -> None:
    connected = _agent(
        "connected",
        system={"reachable": True, "system_state": "online"},
        jellyfin={"reachable": False, "error": "backend_unavailable"},
    )
    assert (
        BetaAssistantService._nas_power_state(
            _status("reachable", "reachable"), connected
        )
        == "online"
    )
    assert (
        BetaAssistantService._nas_lifecycle(
            _status("reachable", "reachable"), connected, None
        )
        == "AGENT_CONNECTED"
    )

    shutting_down = _agent(
        "disconnected",
        shutdown_accepted_at=1_700_000_000.0,
    )
    assert (
        BetaAssistantService._nas_power_state(
            _status("reachable", "unreachable"), shutting_down
        )
        == "shutting_down"
    )
    assert (
        BetaAssistantService._nas_lifecycle(
            _status("reachable", "unreachable"), shutting_down, None
        )
        == "SHUTTING_DOWN"
    )
    assert (
        BetaAssistantService._nas_power_state(
            _status("unreachable", "unreachable"), shutting_down
        )
        == "off"
    )


def test_wake_lifecycle_is_observation_driven() -> None:
    disconnected = _agent("disconnected")
    wake = {"operation": "wake_nas", "at": 1_700_000_000.0}
    assert (
        BetaAssistantService._nas_lifecycle(
            _status("unreachable", "reachable"), disconnected, wake
        )
        == "NAS_REACHABLE"
    )
    assert (
        BetaAssistantService._nas_lifecycle(
            _status("unreachable", "unreachable"), disconnected, wake
        )
        == "OFF"
    )
