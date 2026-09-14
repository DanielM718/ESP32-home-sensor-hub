"""Server-side NAS, Tailscale, and Jellyfin observation.

Every probe destination comes from operator configuration
(:class:`butters.assistant_config.NasEndpointSettings`). No browser, portal, or
action request supplies, overrides, or influences a host, address, port, or URL
here, and there is no generic reachability entry point: the four probes below
are the only network destinations this module can ever reach.

The four observations stay independent on purpose. "Jellyfin did not answer" is
not evidence that the NAS is powered off, and this module never collapses them
into that claim -- the aggregate below is a presentation of the observations,
not a replacement for them.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from butters.assistant_config import NasEndpointSettings


class NasAggregate(str, Enum):
    """User-facing summary of the independent observations."""

    OFFLINE = "OFFLINE"
    WAKING = "WAKING"
    NAS_REACHABLE = "NAS_REACHABLE"
    TAILSCALE_REACHABLE = "TAILSCALE_REACHABLE"
    JELLYFIN_STARTING = "JELLYFIN_STARTING"
    READY = "READY"
    UNKNOWN = "UNKNOWN"


class Reach(str, Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    NOT_OBSERVED = "not_observed"


class JellyfinState(str, Enum):
    READY = "ready"
    STARTING = "starting"
    UNAVAILABLE = "unavailable"
    NOT_OBSERVED = "not_observed"


@dataclass(frozen=True, slots=True)
class NasObservation:
    """One bounded, independently-sourced view of the configured NAS."""

    lan: Reach
    nas_api: Reach
    tailscale: Reach
    jellyfin: JellyfinState
    aggregate: NasAggregate
    observed_at: float
    probe_seconds: float
    # Only set when the caller passed a recent wake record; never inferred.
    wake_grace_active: bool = False

    def safe_dict(self) -> dict[str, object]:
        return {
            "observations": {
                "lan": self.lan.value,
                "nas_api": self.nas_api.value,
                "tailscale": self.tailscale.value,
                "jellyfin": self.jellyfin.value,
            },
            "aggregate": self.aggregate.value,
            "observed_at": self.observed_at,
            "probe_seconds": round(self.probe_seconds, 3),
            "wake_grace_active": self.wake_grace_active,
        }


def _aggregate(
    lan: Reach, nas_api: Reach, tailscale: Reach, jellyfin: JellyfinState, waking: bool
) -> NasAggregate:
    """Present the observations truthfully; never infer power state.

    Order matters only for presentation. The caller still receives all four
    observations, so a consumer that cares about, say, Tailscale specifically
    never has to read it back out of this summary.
    """

    if jellyfin is JellyfinState.READY:
        return NasAggregate.READY
    reachable = {lan, nas_api, tailscale}
    if jellyfin is JellyfinState.STARTING or (
        Reach.REACHABLE in reachable and jellyfin is JellyfinState.UNAVAILABLE
    ):
        return NasAggregate.JELLYFIN_STARTING
    if nas_api is Reach.REACHABLE or lan is Reach.REACHABLE:
        return NasAggregate.NAS_REACHABLE
    if tailscale is Reach.REACHABLE:
        return NasAggregate.TAILSCALE_REACHABLE
    if waking:
        return NasAggregate.WAKING
    if Reach.REACHABLE not in reachable and Reach.UNREACHABLE in reachable:
        return NasAggregate.OFFLINE
    return NasAggregate.UNKNOWN


class NasStatusObserver:
    """Bounded probes against the configured NAS only.

    A short result cache keeps portal polling from multiplying into per-viewer
    network load, and every probe carries both its own timeout and a share of
    one overall budget so a black-holed address cannot stall the request.
    """

    def __init__(
        self,
        settings: NasEndpointSettings,
        *,
        connector: Callable[..., Any] = socket.create_connection,
        opener: Callable[..., Any] = urlopen,
        ping: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.connector = connector
        self.opener = opener
        self.ping = ping
        self.clock = clock
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._cached: NasObservation | None = None
        self._cached_at = 0.0

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def observe(
        self, *, wake_requested_at: float | None = None, refresh: bool = False
    ) -> NasObservation:
        waking = self._wake_grace(wake_requested_at)
        with self._lock:
            cached = self._cached
            fresh = (
                cached is not None
                and not refresh
                and (self.monotonic() - self._cached_at) < self.settings.cache_seconds
            )
        if fresh and cached is not None:
            return NasObservation(
                cached.lan,
                cached.nas_api,
                cached.tailscale,
                cached.jellyfin,
                _aggregate(
                    cached.lan, cached.nas_api, cached.tailscale, cached.jellyfin, waking
                ),
                cached.observed_at,
                cached.probe_seconds,
                waking,
            )
        observation = self._probe(waking)
        with self._lock:
            self._cached = observation
            self._cached_at = self.monotonic()
        return observation

    def _wake_grace(self, wake_requested_at: float | None) -> bool:
        if wake_requested_at is None:
            return False
        return (self.clock() - wake_requested_at) <= self.settings.wake_grace_seconds

    def _probe(self, waking: bool) -> NasObservation:
        started = self.monotonic()
        deadline = started + self.settings.total_probe_seconds
        lan = self._ping(self.settings.lan_host, deadline)
        nas_api = self._tcp(self.settings.lan_host, self.settings.api_port, deadline)
        tailscale = self._tcp(
            self.settings.tailscale_host, self.settings.tailscale_probe_port, deadline
        )
        jellyfin = self._jellyfin(lan, tailscale, deadline)
        elapsed = self.monotonic() - started
        return NasObservation(
            lan,
            nas_api,
            tailscale,
            jellyfin,
            _aggregate(lan, nas_api, tailscale, jellyfin, waking),
            self.clock(),
            elapsed,
            waking,
        )

    def _budget(self, deadline: float) -> float:
        return min(self.settings.probe_timeout_seconds, deadline - self.monotonic())

    def _ping(self, host: str, deadline: float) -> Reach:
        if not host:
            return Reach.NOT_OBSERVED
        budget = self._budget(deadline)
        if budget <= 0:
            return Reach.NOT_OBSERVED
        try:
            result = self.ping(
                ["/usr/bin/ping", "-c", "1", "-W", "1", host],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=budget,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return Reach.UNREACHABLE
        return (
            Reach.REACHABLE
            if int(getattr(result, "returncode", 1)) == 0
            else Reach.UNREACHABLE
        )

    def _tcp(self, host: str, port: int, deadline: float) -> Reach:
        if not host:
            return Reach.NOT_OBSERVED
        budget = self._budget(deadline)
        if budget <= 0:
            return Reach.NOT_OBSERVED
        try:
            connection = self.connector((host, port), timeout=budget)
        except (OSError, TimeoutError):
            return Reach.UNREACHABLE
        close = getattr(connection, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass
        return Reach.REACHABLE

    def _jellyfin(self, lan: Reach, tailscale: Reach, deadline: float) -> JellyfinState:
        """Probe readiness on whichever configured base the server can reach.

        The base URL is chosen here, server-side, from the two configured
        destinations only. A caller never names the endpoint, and a failure is
        reported as a Jellyfin observation -- never as a NAS power claim.
        """

        base = ""
        if lan is Reach.REACHABLE and self.settings.jellyfin_lan_url:
            base = self.settings.jellyfin_lan_url
        elif tailscale is Reach.REACHABLE and self.settings.jellyfin_tailscale_url:
            base = self.settings.jellyfin_tailscale_url
        elif self.settings.jellyfin_lan_url:
            base = self.settings.jellyfin_lan_url
        if not base:
            return JellyfinState.NOT_OBSERVED
        budget = self._budget(deadline)
        if budget <= 0:
            return JellyfinState.NOT_OBSERVED
        request = Request(
            base.rstrip("/") + self.settings.jellyfin_readiness_path,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=budget) as response:
                status = int(getattr(response, "status", 200))
                # Read and discard a bounded prefix so a dripping body cannot
                # hold the connection past the probe budget.
                read = getattr(response, "read", None)
                if callable(read):
                    read(4096)
        except HTTPError as exc:
            # The port answered, so something is up but not serving readiness.
            return (
                JellyfinState.STARTING
                if exc.code in {500, 502, 503, 504}
                else JellyfinState.UNAVAILABLE
            )
        except (URLError, TimeoutError, OSError, ValueError):
            return JellyfinState.UNAVAILABLE
        if status == 200:
            return JellyfinState.READY
        if status in {500, 502, 503, 504}:
            return JellyfinState.STARTING
        return JellyfinState.UNAVAILABLE
