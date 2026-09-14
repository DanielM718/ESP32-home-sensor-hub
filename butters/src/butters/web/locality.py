"""Server-side locality classification for the Jellyfin redirect.

THE TRUST BOUNDARY
------------------
The only thing this module trusts is the local ingress. Concretely:

1. The request must arrive on the loopback socket from a peer in
   ``trusted_peers`` -- in production, Tailscale Serve or a reviewed local
   reverse proxy on the same host. A request that reaches the daemon any other
   way is classified ``UNKNOWN`` and gets the fail-safe destination.
2. Only inside that boundary is an ingress-stated locality header read, and
   only when an operator configured ``portal.locality_header``. Nothing a
   browser can set is consulted: ``X-Forwarded-For``, ``X-Real-IP``,
   ``Tailscale-User-Login`` supplied by a client, ``?local=true``, and every
   other caller-controlled value are ignored for this decision. A client that
   forges the configured header name still fails step 1 unless it is already
   the trusted loopback ingress, which is the proxy itself.
3. With no ingress-stated locality, the server may consult its OWN tailscaled
   for the current endpoint of the peer that Serve says is calling. That is a
   local read of local daemon state, not client input.

Anything that cannot be established this way is ``UNKNOWN``, and ``UNKNOWN``
resolves to the Tailscale destination. The failure mode is "remote user takes
the overlay path", never "arbitrary redirect".

There is no code path in this module that can produce a destination that is not
one of the two operator-configured Jellyfin URLs.
"""

from __future__ import annotations

import ipaddress
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from butters.assistant_config import NasEndpointSettings, PortalSettings


class Locality(str, Enum):
    LAN = "lan"
    TAILNET = "tailnet"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class LocalityDecision:
    locality: Locality
    # How the classification was reached, for the admin surface and audit only.
    source: str

    def safe_dict(self) -> dict[str, object]:
        return {"locality": self.locality.value, "source": self.source}


_STATED = {"lan": Locality.LAN, "tailnet": Locality.TAILNET}


class LocalityClassifier:
    def __init__(
        self,
        settings: PortalSettings,
        *,
        trusted_peers: frozenset[str],
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.settings = settings
        self.trusted_peers = trusted_peers
        self.runner = runner
        self._networks = tuple(
            ipaddress.ip_network(item, strict=False)
            for item in settings.lan_networks
        )

    def classify(self, headers: object, client_host: str | None) -> LocalityDecision:
        if client_host not in self.trusted_peers:
            # Not the reviewed local ingress: nothing about this request may be
            # believed, including whatever it claims about itself.
            return LocalityDecision(Locality.UNKNOWN, "untrusted_ingress")
        stated = self._stated(headers)
        if stated is not None:
            return LocalityDecision(stated, "trusted_ingress_header")
        if not self._networks or not self.settings.tailscale_status_command:
            return LocalityDecision(Locality.UNKNOWN, "no_classifier_configured")
        login = _header(headers, "tailscale-user-login")
        if not login:
            return LocalityDecision(Locality.UNKNOWN, "no_ingress_identity")
        return self._from_tailscaled(login)

    def _stated(self, headers: object) -> Locality | None:
        if not self.settings.locality_header:
            return None
        value = _header(headers, self.settings.locality_header)
        if value is None:
            return None
        return _STATED.get(value.strip().casefold())

    def _from_tailscaled(self, login: str) -> LocalityDecision:
        """Ask the local tailscaled where this login's active peers are.

        A peer whose current direct endpoint sits inside a configured home
        network is on the LAN. Anything else -- relayed, a different network, no
        endpoint yet, several peers disagreeing, or a daemon that will not
        answer -- is UNKNOWN, which is the fail-safe.
        """

        try:
            result = self.runner(
                list(self.settings.tailscale_status_command),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        if int(getattr(result, "returncode", 1)) != 0:
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        raw = str(getattr(result, "stdout", ""))
        if len(raw) > 1_000_000:
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        if not isinstance(payload, dict):
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        users = payload.get("User")
        peers = payload.get("Peer")
        if not isinstance(peers, dict):
            return LocalityDecision(Locality.UNKNOWN, "classifier_unavailable")
        user_ids = {
            str(key)
            for key, value in (users or {}).items()
            if isinstance(value, dict)
            and str(value.get("LoginName", "")).casefold() == login.casefold()
        }
        if not user_ids:
            return LocalityDecision(Locality.UNKNOWN, "no_matching_peer")
        endpoints: list[str] = []
        for peer in peers.values():
            if not isinstance(peer, dict) or not peer.get("Online"):
                continue
            if str(peer.get("UserID")) not in user_ids:
                continue
            address = peer.get("CurAddr")
            if isinstance(address, str) and address:
                endpoints.append(address)
        if not endpoints:
            return LocalityDecision(Locality.UNKNOWN, "no_direct_endpoint")
        if all(self._in_home_network(item) for item in endpoints):
            return LocalityDecision(Locality.LAN, "tailscaled_endpoint")
        return LocalityDecision(Locality.TAILNET, "tailscaled_endpoint")

    def _in_home_network(self, endpoint: str) -> bool:
        host = endpoint.rsplit(":", 1)[0].strip("[]")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return any(address in network for network in self._networks)


def jellyfin_destination(
    decision: LocalityDecision, endpoints: NasEndpointSettings
) -> str:
    """Pick one of exactly two operator-configured URLs.

    LAN gets the LAN URL for latency and throughput; everything else, including
    every unclassifiable request, gets the Tailscale URL. When a configured URL
    is missing the other configured URL is used, and when neither is configured
    the caller gets an empty string and must not redirect.
    """

    if decision.locality is Locality.LAN and endpoints.jellyfin_lan_url:
        return endpoints.jellyfin_lan_url
    if endpoints.jellyfin_tailscale_url:
        return endpoints.jellyfin_tailscale_url
    return endpoints.jellyfin_lan_url


def _header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return value.strip() if isinstance(value, str) and value.strip() else None
