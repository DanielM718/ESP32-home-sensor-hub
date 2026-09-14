"""Deterministic NAS status observation.

The properties under test are the ones the model exists to guarantee: the four
observations stay independent, a silent Jellyfin is never reported as a powered
off NAS, probe destinations come only from configuration, and every probe is
bounded.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

from butters.assistant_config import NasEndpointSettings
from butters.integrations.nas_status import (
    JellyfinState,
    NasAggregate,
    NasStatusObserver,
    Reach,
)

SETTINGS = NasEndpointSettings(
    lan_host="192.168.1.50",
    api_url="https://192.168.1.50",
    tailscale_host="nas.tail0000.ts.net",
    jellyfin_lan_url="http://192.168.1.50:8096",
    jellyfin_tailscale_url="https://nas.tail0000.ts.net",
    cache_seconds=0.0,
).validated()


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def read(self, _size):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Probes:
    """Records every destination the observer reaches for."""

    def __init__(self, *, ping=True, tcp=None, http=200) -> None:
        self.ping_result = ping
        self.tcp_result = {} if tcp is None else tcp
        self.http = http
        self.pinged: list[str] = []
        self.connected: list[tuple[str, int]] = []
        self.fetched: list[str] = []

    def run(self, argv, **kwargs):
        self.pinged.append(argv[-1])
        assert "timeout" in kwargs and kwargs["timeout"] > 0
        return subprocess.CompletedProcess(argv, 0 if self.ping_result else 1)

    def connect(self, address, timeout=None):
        assert timeout is not None and timeout > 0
        self.connected.append(address)
        if not self.tcp_result.get(address[0], True):
            raise OSError("refused")

        class _Socket:
            def close(self_inner):
                return None

        return _Socket()

    def open(self, request, timeout=None):
        assert timeout is not None and timeout > 0
        self.fetched.append(request.full_url)
        if isinstance(self.http, Exception):
            raise self.http
        return _Response(self.http)


def _observer(probes: _Probes, settings: NasEndpointSettings = SETTINGS):
    return NasStatusObserver(
        settings,
        connector=probes.connect,
        opener=probes.open,
        ping=probes.run,
    )


def test_all_probes_reachable_and_jellyfin_ready_is_ready() -> None:
    probes = _Probes()
    observation = _observer(probes).observe()
    assert observation.lan is Reach.REACHABLE
    assert observation.nas_api is Reach.REACHABLE
    assert observation.tailscale is Reach.REACHABLE
    assert observation.jellyfin is JellyfinState.READY
    assert observation.aggregate is NasAggregate.READY


def test_nothing_reachable_is_offline() -> None:
    probes = _Probes(
        ping=False,
        tcp={"192.168.1.50": False, "nas.tail0000.ts.net": False},
        http=OSError("down"),
    )
    observation = _observer(probes).observe()
    assert observation.aggregate is NasAggregate.OFFLINE


def test_lan_reachable_without_jellyfin_is_not_reported_as_powered_off() -> None:
    """The core truthfulness property: Jellyfin down never means NAS off."""

    probes = _Probes(http=OSError("connection refused"))
    observation = _observer(probes).observe()
    assert observation.lan is Reach.REACHABLE
    assert observation.jellyfin is JellyfinState.UNAVAILABLE
    assert observation.aggregate is NasAggregate.JELLYFIN_STARTING
    assert observation.aggregate is not NasAggregate.OFFLINE


def test_tailscale_only_reachability_is_reported_separately() -> None:
    probes = _Probes(
        ping=False,
        tcp={"192.168.1.50": False, "nas.tail0000.ts.net": True},
        http=OSError("down"),
    )
    observation = _observer(probes).observe()
    assert observation.lan is Reach.UNREACHABLE
    assert observation.nas_api is Reach.UNREACHABLE
    assert observation.tailscale is Reach.REACHABLE
    # Reachable over the overlay with Jellyfin silent is a services problem.
    assert observation.aggregate is NasAggregate.JELLYFIN_STARTING


def test_nas_reachable_but_jellyfin_not_configured_reports_nas_reachable() -> None:
    settings = replace(
        SETTINGS, jellyfin_lan_url="", jellyfin_tailscale_url=""
    ).validated()
    probes = _Probes()
    observation = _observer(probes, settings).observe()
    assert observation.jellyfin is JellyfinState.NOT_OBSERVED
    assert observation.aggregate is NasAggregate.NAS_REACHABLE
    assert probes.fetched == []


def test_jellyfin_server_error_is_starting_not_unavailable() -> None:
    probes = _Probes(http=503)
    observation = _observer(probes).observe()
    assert observation.jellyfin is JellyfinState.STARTING
    assert observation.aggregate is NasAggregate.JELLYFIN_STARTING


def test_recent_wake_with_nothing_reachable_reports_waking() -> None:
    import time

    probes = _Probes(
        ping=False,
        tcp={"192.168.1.50": False, "nas.tail0000.ts.net": False},
        http=OSError("down"),
    )
    observation = _observer(probes).observe(wake_requested_at=time.time())
    assert observation.aggregate is NasAggregate.WAKING
    assert observation.wake_grace_active is True


def test_expired_wake_grace_falls_back_to_offline() -> None:
    import time

    probes = _Probes(
        ping=False,
        tcp={"192.168.1.50": False, "nas.tail0000.ts.net": False},
        http=OSError("down"),
    )
    stale = time.time() - SETTINGS.wake_grace_seconds - 1
    observation = _observer(probes).observe(wake_requested_at=stale)
    assert observation.aggregate is NasAggregate.OFFLINE
    assert observation.wake_grace_active is False


def test_unconfigured_targets_are_not_observed_rather_than_unreachable() -> None:
    settings = NasEndpointSettings(lan_host="192.168.1.50").validated()
    probes = _Probes()
    observation = _observer(probes, settings).observe()
    assert observation.tailscale is Reach.NOT_OBSERVED
    assert observation.jellyfin is JellyfinState.NOT_OBSERVED
    assert probes.connected == [("192.168.1.50", 443)]


def test_probe_destinations_come_only_from_configuration() -> None:
    """No caller can steer a probe: observe() takes no destination at all."""

    probes = _Probes()
    observer = _observer(probes)
    observation = observer.observe()
    assert probes.pinged == ["192.168.1.50"]
    assert probes.connected == [
        ("192.168.1.50", 443),
        ("nas.tail0000.ts.net", 443),
    ]
    assert probes.fetched == ["http://192.168.1.50:8096/health"]
    assert observation.aggregate is NasAggregate.READY
    # The public surface accepts a wake timestamp and a cache bypass, and
    # nothing that could name or influence a destination.
    import inspect

    parameters = set(inspect.signature(observer.observe).parameters)
    assert parameters == {"wake_requested_at", "refresh"}


def test_total_probe_budget_is_bounded() -> None:
    """A slow first probe must not let later probes run past the budget."""

    settings = replace(
        SETTINGS, total_probe_seconds=1.0, probe_timeout_seconds=2.0
    ).validated()
    probes = _Probes()
    clock = {"value": 0.0}

    def monotonic():
        clock["value"] += 0.6
        return clock["value"]

    observer = NasStatusObserver(
        settings,
        connector=probes.connect,
        opener=probes.open,
        ping=probes.run,
        monotonic=monotonic,
    )
    observation = observer.observe()
    # The budget expired, so later probes were skipped rather than run long.
    assert Reach.NOT_OBSERVED in {observation.nas_api, observation.tailscale}
    assert observation.aggregate is not NasAggregate.READY


def test_results_are_cached_for_the_configured_window() -> None:
    settings = replace(SETTINGS, cache_seconds=30.0).validated()
    probes = _Probes()
    observer = _observer(probes, settings)
    observer.observe()
    observer.observe()
    assert probes.pinged == ["192.168.1.50"]
    observer.observe(refresh=True)
    assert probes.pinged == ["192.168.1.50", "192.168.1.50"]


def test_socket_failures_are_unreachable_not_exceptions() -> None:
    class _Broken(_Probes):
        def connect(self, address, timeout=None):
            raise TimeoutError("timed out")

    observation = _observer(_Broken()).observe()
    assert observation.nas_api is Reach.UNREACHABLE
    assert observation.tailscale is Reach.UNREACHABLE
