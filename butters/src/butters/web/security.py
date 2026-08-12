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


class SecurityError(PermissionError):
    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class AuthPolicy:
    """Trust Tailscale identity only for a configured loopback proxy topology."""

    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings
        environment_identities = tuple(
            value.strip()
            for value in os.getenv("BUTTERS_ADMIN_IDENTITIES", "").split(",")
            if value.strip()
        )
        self.admin_identities = frozenset(
            value.casefold()
            for value in (*settings.admin_identities, *environment_identities)
        )

    def admin_identity(
        self,
        headers: object,
        client_host: str | None,
    ) -> str:
        if (
            self.settings.development_mode
            and os.getenv("BUTTERS_DEV_ADMIN") == "1"
            and client_host in {"127.0.0.1", "::1", "testclient"}
        ):
            return "development-local-admin"
        if not self.settings.trusted_tailscale_proxy:
            raise SecurityError("identity_proxy_disabled", "administrator identity proxy is disabled")
        if self.settings.host not in {"127.0.0.1", "::1", "localhost"}:
            raise SecurityError("unsafe_proxy_topology", "administrator identity cannot be trusted")
        if client_host not in {"127.0.0.1", "::1", "testclient"}:
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

    def require_origin(self, headers: object, host: str | None) -> str:
        origin = _header(headers, "origin")
        if not origin:
            raise SecurityError("origin_missing", "an Origin header is required")
        parsed = urlparse(origin)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise SecurityError("origin_invalid", "request origin is invalid")
        if self.settings.allowed_origins:
            allowed = {item.rstrip("/") for item in self.settings.allowed_origins}
            if origin.rstrip("/") not in allowed:
                raise SecurityError("origin_denied", "request origin is not allowed")
        elif host and parsed.netloc.casefold() != host.casefold():
            raise SecurityError("origin_denied", "request is not same-origin")
        if not self.settings.development_mode and parsed.scheme != "https":
            raise SecurityError("https_required", "production mutations require HTTPS")
        return origin

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
