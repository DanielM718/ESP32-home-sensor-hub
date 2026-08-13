"""Tailscale-proxy admin authorization, origin checks, CSRF, and rate limits."""

from __future__ import annotations

import hmac
import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from butters.assistant_config import WebSettings
from butters.web.sessions import BrowserSession


# Only real loopback peers are trusted to carry proxy-supplied identity. Test
# harnesses inject their own peer name through AuthPolicy(trusted_peers=...)
# so no test-only identity is compiled into the deployed authorization path.
DEFAULT_TRUSTED_PEERS = frozenset({"127.0.0.1", "::1"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class SecurityError(PermissionError):
    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AuthPolicy:
    """Trust Tailscale identity only for a configured loopback proxy topology."""

    def __init__(
        self,
        settings: WebSettings,
        *,
        trusted_peers: frozenset[str] | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings
        source = os.environ if environment is None else environment
        environment_identities = tuple(
            value.strip()
            for value in source.get("BUTTERS_ADMIN_IDENTITIES", "").split(",")
            if value.strip()
        )
        self.admin_identities = frozenset(
            value.casefold()
            for value in (*settings.admin_identities, *environment_identities)
        )
        self.trusted_peers = DEFAULT_TRUSTED_PEERS if trusted_peers is None else trusted_peers
        self._dev_admin = source.get("BUTTERS_DEV_ADMIN") == "1"

    def admin_identity(
        self,
        headers: object,
        client_host: str | None,
    ) -> str:
        if (
            self.settings.development_mode
            and self._dev_admin
            and client_host in self.trusted_peers
        ):
            return "development-local-admin"
        if not self.settings.trusted_tailscale_proxy:
            raise SecurityError("identity_proxy_disabled", "administrator identity proxy is disabled")
        if self.settings.host not in LOOPBACK_HOSTS:
            raise SecurityError("unsafe_proxy_topology", "administrator identity cannot be trusted")
        if client_host not in self.trusted_peers:
            raise SecurityError(
                "untrusted_proxy_peer",
                "administrator identity headers are accepted only from the loopback proxy",
            )
        identity = _header(headers, "tailscale-user-login")
        if not identity:
            raise SecurityError("admin_identity_missing", "administrator identity is required")
        if not self.admin_identities or identity.casefold() not in self.admin_identities:
            raise SecurityError("admin_identity_denied", "administrator identity is not authorized")
        return identity

    def is_administrator(self, headers: object, client_host: str | None) -> bool:
        try:
            self.admin_identity(headers, client_host)
        except SecurityError:
            return False
        return True

    def peer_key(self, headers: object, client_host: str | None) -> str:
        """Identify the caller for admission control.

        Every browser reaches the daemon through the same loopback proxy, so the
        socket peer is useless for fairness. The proxy-supplied tailnet login is
        the only per-caller value available, and it falls back to the socket peer
        for direct callers.
        """

        if (
            self.settings.trusted_tailscale_proxy
            and client_host in self.trusted_peers
            and self.settings.host in LOOPBACK_HOSTS
        ):
            identity = _header(headers, "tailscale-user-login")
            if identity:
                return "identity:" + identity.casefold()[:128]
        return "peer:" + str(client_host or "unknown")[:64]

    def require_origin(self, headers: object, host: str | None) -> str:
        origin = _header(headers, "origin")
        if not origin:
            raise SecurityError("origin_missing", "an Origin header is required")
        parsed = urlparse(origin)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise SecurityError("origin_invalid", "request origin is invalid")
        if self.settings.allowed_origins:
            allowed = {item.rstrip("/").casefold() for item in self.settings.allowed_origins}
            if origin.rstrip("/").casefold() not in allowed:
                raise SecurityError("origin_denied", "request origin is not allowed")
        elif self.settings.development_mode:
            # Development has no server-known public origin; comparing against the
            # request Host is convenience only and is never used in production.
            if host and parsed.netloc.casefold() != host.casefold():
                raise SecurityError("origin_denied", "request is not same-origin")
        else:
            raise SecurityError(
                "origin_not_configured",
                "web.allowed_origins must list the private HTTPS origin before mutations are accepted",
                503,
            )
        if not self.settings.development_mode and parsed.scheme != "https":
            raise SecurityError("https_required", "production mutations require HTTPS")
        return origin

    def require_browser_context(self, headers: object) -> None:
        """Admission gate for the cookie-less session allocation request.

        Browsers label same-origin `fetch` with `Sec-Fetch-Site: same-origin`;
        an arbitrary tailnet or loopback script does not. Production therefore
        requires either that label or an explicitly allow-listed Origin, so a
        non-browser flood cannot silently consume the session pool.
        """

        if self.settings.development_mode:
            return
        if not self.settings.allowed_origins:
            raise SecurityError(
                "origin_not_configured",
                "web.allowed_origins must list the private HTTPS origin before sessions are issued",
                503,
            )
        # same-origin only. "same-site" would admit any other node under the
        # same registrable tailnet domain, which is not the trust boundary here.
        if _header(headers, "sec-fetch-site") == "same-origin":
            return
        origin = _header(headers, "origin")
        if origin:
            self.require_origin(headers, None)
            return
        raise SecurityError(
            "browser_context_required",
            "session allocation requires a same-origin browser request",
        )

    @staticmethod
    def require_csrf(session: BrowserSession, supplied: str | None) -> None:
        if not supplied or not hmac.compare_digest(session.csrf_token, supplied):
            raise SecurityError("csrf_denied", "CSRF token is missing or invalid")


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Small in-process token-bucket limiter with bounded key retention."""

    def __init__(self, *, rate_per_minute: float, burst: int, max_keys: int = 512) -> None:
        self.rate_per_second = rate_per_minute / 60.0
        self.burst = float(burst)
        self.max_keys = max_keys
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    @property
    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._buckets)

    def check(self, key: str, *, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    oldest = min(self._buckets, key=lambda item: self._buckets[item].updated)
                    self._buckets.pop(oldest, None)
                bucket = _Bucket(self.burst, now)
                self._buckets[key] = bucket
            bucket.tokens = min(
                self.burst,
                bucket.tokens + (now - bucket.updated) * self.rate_per_second,
            )
            bucket.updated = now
            if bucket.tokens < cost:
                return False
            bucket.tokens -= cost
            return True


def _header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return value.strip() if isinstance(value, str) and value.strip() else None
