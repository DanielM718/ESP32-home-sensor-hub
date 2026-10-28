from __future__ import annotations

from dataclasses import replace

from butters.integrations.display_topology import (
    DisplayIdentity,
    DisplayState,
    DisplayTopologyGuard,
    SunshineDisconnectGuard,
)


VIRTUAL = DisplayIdentity(
    "{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}",
    r"DISPLAY\MTT1337\1&15ECD195&4&UID256",
    "MTT",
    "1337",
    "518463207",
    True,
)
GIGABYTE = DisplayIdentity(
    "{379ade36-a9ac-587a-b130-9bffdcab7058}",
    r"DISPLAY\GBT270B\5&19CEA493&0&UID4353",
    "GBT",
    "270B",
    "158",
    False,
)
OLED = DisplayIdentity(
    "{5d53b114-72f4-51ef-b227-befdc81192db}",
    r"DISPLAY\GBT3214\5&19CEA493&0&UID4357",
    "GBT",
    "3214",
    "",
    False,
)


def state(identity, active, display_name):
    return DisplayState(identity, active, display_name)


def guard():
    return DisplayTopologyGuard(
        virtual_identity=VIRTUAL, physical_identities=(GIGABYTE, OLED)
    )


def topology(*, virtual_name="DISPLAY94", gigabyte=False, oled=False):
    return (
        state(VIRTUAL, True, virtual_name),
        state(GIGABYTE, gigabyte, "DISPLAY1"),
        state(OLED, oled, "DISPLAY2"),
    )


def test_virtual_display_number_change_does_not_change_identity() -> None:
    expected = guard()
    assert expected.assess(topology(virtual_name="DISPLAY94")).ready is True
    changed = expected.assess(topology(virtual_name="DISPLAY26"))
    assert changed.ready is True
    assert changed.virtual_display.display_name == "DISPLAY26"


def test_physical_and_virtual_indexes_can_swap_without_identity_collision() -> None:
    values = (
        state(VIRTUAL, True, "DISPLAY1"),
        state(GIGABYTE, True, "DISPLAY94"),
        state(OLED, False, "DISPLAY2"),
    )
    assert guard().assess(values).ready is True


def test_virtual_display_disappearance_fails_closed() -> None:
    values = tuple(item for item in topology() if item.identity != VIRTUAL)
    assert guard().assess(values).code == "virtual_display_missing"


def test_physical_display_disappearance_fails_closed() -> None:
    values = tuple(item for item in topology() if item.identity != OLED)
    assert guard().assess(values).code == "physical_display_missing"


def test_ambiguous_reenumerated_identity_fails_closed() -> None:
    duplicate = replace(state(VIRTUAL, True, "DISPLAY27"), identity=VIRTUAL)
    assert guard().assess(topology() + (duplicate,)).code == "display_identity_ambiguous"


def test_physical_monitor_enabled_midstream_is_never_restored_away() -> None:
    before = topology(gigabyte=False, oled=False)
    current = topology(virtual_name="DISPLAY26", gigabyte=True, oled=False)
    decision = SunshineDisconnectGuard(guard()).decide(
        "disconnect-1",
        stream_start=before,
        current=current,
        remote_generation=4,
        local_generation=4,
    )
    assert decision.action == "leave_unchanged"
    assert decision.reason == "physical_display_changed"


def test_both_physical_monitors_active_are_left_unchanged() -> None:
    values = topology(gigabyte=True, oled=True)
    decision = SunshineDisconnectGuard(guard()).decide(
        "disconnect-2",
        stream_start=values,
        current=values,
        remote_generation=7,
        local_generation=7,
    )
    assert decision.action == "leave_unchanged"
    assert decision.reason == "sunshine_topology_mutation_disabled"


def test_moonlight_disconnect_after_local_change_preserves_local_precedence() -> None:
    values = topology(gigabyte=True)
    decision = SunshineDisconnectGuard(guard()).decide(
        "disconnect-3",
        stream_start=topology(),
        current=values,
        remote_generation=8,
        local_generation=9,
    )
    assert decision.reason == "local_mode_precedence"


def test_stale_restoration_snapshot_is_rejected() -> None:
    newly_enumerated_display = DisplayIdentity(
        "{00000000-0000-0000-0000-000000000004}",
        r"DISPLAY\NEW1234\5&NEWPATH&0&UID5000",
        "NEW",
        "1234",
        "5000",
        False,
    )
    current = topology(virtual_name="DISPLAY26") + (
        state(newly_enumerated_display, False, "DISPLAY4"),
    )
    assert (
        SunshineDisconnectGuard(guard())
        .decide(
            "disconnect-4",
            stream_start=topology(),
            current=current,
            remote_generation=10,
            local_generation=10,
        )
        .reason
        == "stale_topology_snapshot"
    )


def test_repeated_disconnect_notification_is_idempotent() -> None:
    values = topology()
    cleanup = SunshineDisconnectGuard(guard())
    first = cleanup.decide(
        "disconnect-5",
        stream_start=values,
        current=values,
        remote_generation=11,
        local_generation=11,
    )
    second = cleanup.decide(
        "disconnect-5",
        stream_start=values,
        current=values,
        remote_generation=11,
        local_generation=11,
    )
    assert first.duplicate is False
    assert second.duplicate is True
    assert second.reason == "duplicate_disconnect"
