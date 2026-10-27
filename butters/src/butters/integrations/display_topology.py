"""Fail-closed Windows display identity and disconnect-cleanup policy.

Display numbers and primary-display state are observations, not identities.  The
stable boundary is the Sunshine device ID plus the Windows monitor instance path
and EDID tuple.  Sunshine is configured in verification-only mode, so this policy
never restores a saved topology on disconnect; it records why leaving the live
topology unchanged is required.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DisplayIdentity:
    sunshine_device_id: str
    instance_path: str
    manufacturer: str
    product_code: str
    serial: str
    virtual: bool

    @property
    def key(self) -> tuple[str, str, str, str, str, bool]:
        return (
            self.sunshine_device_id.casefold(),
            self.instance_path.casefold(),
            self.manufacturer.casefold(),
            self.product_code.casefold(),
            self.serial.casefold(),
            self.virtual,
        )


@dataclass(frozen=True, slots=True)
class DisplayState:
    identity: DisplayIdentity
    active: bool
    # Informational only.  This may change from DISPLAY94 to DISPLAY26 or swap
    # with a physical output without changing identity.
    display_name: str = ""


@dataclass(frozen=True, slots=True)
class TopologyAssessment:
    ready: bool
    code: str
    virtual_display: DisplayState | None = None


@dataclass(frozen=True, slots=True)
class DisconnectDecision:
    action: str
    reason: str
    duplicate: bool = False


class DisplayTopologyGuard:
    """Validate one fixed virtual display and both fixed physical monitors."""

    def __init__(
        self,
        *,
        virtual_identity: DisplayIdentity,
        physical_identities: tuple[DisplayIdentity, ...],
    ) -> None:
        if not virtual_identity.virtual or not physical_identities:
            raise ValueError("display identity policy is invalid")
        if any(identity.virtual for identity in physical_identities):
            raise ValueError("physical display identity is marked virtual")
        self.virtual_identity = virtual_identity
        self.physical_identities = physical_identities

    def assess(self, displays: tuple[DisplayState, ...]) -> TopologyAssessment:
        device_ids = [item.identity.sunshine_device_id.casefold() for item in displays]
        instance_paths = [item.identity.instance_path.casefold() for item in displays]
        if len(device_ids) != len(set(device_ids)) or len(instance_paths) != len(
            set(instance_paths)
        ):
            return TopologyAssessment(False, "display_identity_ambiguous")

        virtual = [item for item in displays if item.identity.key == self.virtual_identity.key]
        if len(virtual) != 1:
            return TopologyAssessment(
                False,
                "virtual_display_missing"
                if not virtual
                else "virtual_display_ambiguous",
            )
        for expected in self.physical_identities:
            matched = [item for item in displays if item.identity.key == expected.key]
            if len(matched) != 1:
                return TopologyAssessment(
                    False,
                    "physical_display_missing"
                    if not matched
                    else "physical_display_ambiguous",
                    virtual[0],
                )
        if not virtual[0].active:
            return TopologyAssessment(False, "virtual_display_inactive", virtual[0])
        return TopologyAssessment(True, "ready", virtual[0])

    @staticmethod
    def identity_signature(
        displays: tuple[DisplayState, ...]
    ) -> frozenset[tuple[str, str, str, str, str, bool]]:
        return frozenset(item.identity.key for item in displays)

    @staticmethod
    def active_physical_signature(
        displays: tuple[DisplayState, ...]
    ) -> frozenset[tuple[str, str, str, str, str, bool]]:
        return frozenset(
            item.identity.key
            for item in displays
            if item.active and not item.identity.virtual
        )


class SunshineDisconnectGuard:
    """Make disconnect notifications idempotent and topology preserving.

    There is deliberately no ``restore`` action.  With Sunshine in
    verification-only mode Butters owns no pre-stream topology that is safe to
    replay.  A newer Local transition or any identity/activity change makes that
    especially explicit, but even an unchanged topology is left alone.
    """

    def __init__(self, topology: DisplayTopologyGuard) -> None:
        self.topology = topology
        self._handled: set[str] = set()
        self._handled_order: deque[str] = deque()
        self._lock = threading.Lock()

    def decide(
        self,
        event_id: str,
        *,
        stream_start: tuple[DisplayState, ...],
        current: tuple[DisplayState, ...],
        remote_generation: int,
        local_generation: int,
    ) -> DisconnectDecision:
        with self._lock:
            if event_id in self._handled:
                return DisconnectDecision(
                    "leave_unchanged", "duplicate_disconnect", True
                )
            self._handled.add(event_id)
            self._handled_order.append(event_id)
            if len(self._handled_order) > 256:
                self._handled.remove(self._handled_order.popleft())

        assessment = self.topology.assess(current)
        if not assessment.ready:
            return DisconnectDecision("leave_unchanged", assessment.code)
        if local_generation > remote_generation:
            return DisconnectDecision("leave_unchanged", "local_mode_precedence")
        if self.topology.identity_signature(stream_start) != self.topology.identity_signature(
            current
        ):
            return DisconnectDecision("leave_unchanged", "stale_topology_snapshot")
        if self.topology.active_physical_signature(
            stream_start
        ) != self.topology.active_physical_signature(current):
            return DisconnectDecision("leave_unchanged", "physical_display_changed")
        return DisconnectDecision(
            "leave_unchanged", "sunshine_topology_mutation_disabled"
        )
