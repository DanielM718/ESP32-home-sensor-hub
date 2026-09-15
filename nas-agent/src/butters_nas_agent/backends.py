"""Small NAS-local observation and fixed TrueNAS control backends."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import socket
import ssl
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from websockets.sync.client import connect

from .config import AgentConfig
from .protocol import MAX_FRAME, ProtocolError, canonical
from .tls import verify_spki

LOG = logging.getLogger("butters_nas_agent")


def _bounded_text(value: object, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit else None


class TrueNasRpc:
    """JSON-RPC client with four source-owned method names and no generic API."""

    _STATUS_METHODS = ("system.state", "system.version_short", "system.info")
    _SHUTDOWN_METHOD = "system.shutdown"
    _SHUTDOWN_PARAMS = (
        "Butters NAS Agent approved shutdown",
        {"delay": None},
    )
    # A heartbeat and an explicit status action may arrive together. Reuse only
    # a very recent successful snapshot; older observations share the one
    # serialized persistent read session below.
    _STATUS_CACHE_SECONDS = 5.0

    def __init__(
        self,
        config: AgentConfig,
        read_api_key: str,
        shutdown_api_key: str | None = None,
        *,
        connector: Callable[..., Any] = connect,
        pin_verifier: Callable[[object, str], None] = verify_spki,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._read_api_key = read_api_key
        self._shutdown_api_key = shutdown_api_key
        self._connector = connector
        self._pin_verifier = pin_verifier
        self._monotonic = monotonic
        self._status_lock = threading.Lock()
        self._cached_status: tuple[float, dict[str, object]] | None = None
        self._read_websocket: Any | None = None
        self._read_request_id = 1

    def _trace(self, event: str, started: float, **fields: object) -> None:
        LOG.info(
            json.dumps(
                {
                    "event": event,
                    "elapsed_ms": round(
                        max(0.0, self._monotonic() - started) * 1000, 3
                    ),
                    **fields,
                },
                sort_keys=True,
            )
        )

    def _context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _call(
        self,
        websocket: Any,
        request_id: int,
        method: str,
        params: list[object],
        *,
        deadline: float,
    ) -> object:
        if method not in {
            "auth.login_ex",
            *self._STATUS_METHODS,
            self._SHUTDOWN_METHOD,
        }:
            raise ProtocolError("invalid_action")
        started = self._monotonic()
        outcome = "transport_error"
        try:
            websocket.send(
                canonical(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params,
                    }
                ).decode("ascii")
            )
            raw = websocket.recv(timeout=self._remaining(deadline))
            outcome = "response"
        finally:
            self._trace(
                "truenas_rpc",
                started,
                method=method,
                request_id=request_id,
                outcome=outcome,
            )
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_FRAME:
            raise ProtocolError("truenas_unavailable")
        try:
            response = json.loads(raw)
        except (TypeError, ValueError):
            raise ProtocolError("truenas_unavailable") from None
        if (
            type(response) is not dict
            or response.get("jsonrpc") != "2.0"
            or response.get("id") != request_id
            or set(response)
            not in ({"jsonrpc", "id", "result"}, {"jsonrpc", "id", "error"})
        ):
            raise ProtocolError("truenas_unavailable")
        if "error" in response:
            raise ProtocolError("truenas_refused")
        return response["result"]

    def _session(self, api_key: str, *, deadline: float) -> Any:
        hostname = urlsplit(self.config.truenas_url).hostname
        try:
            address_kind = (
                "ip_literal"
                if hostname and ipaddress.ip_address(hostname)
                else "hostname"
            )
        except ValueError:
            address_kind = "hostname"
        connect_started = self._monotonic()
        try:
            websocket = self._connector(
                self.config.truenas_url,
                ssl=self._context(),
                open_timeout=self._remaining(deadline),
                close_timeout=1,
                max_size=MAX_FRAME,
                max_queue=4,
                compression=None,
                proxy=None,
            )
        except Exception:
            self._trace(
                "truenas_connect",
                connect_started,
                outcome="failed",
                address_kind=address_kind,
            )
            raise
        self._trace(
            "truenas_connect",
            connect_started,
            outcome="connected",
            address_kind=address_kind,
        )
        try:
            # The appliance default certificate is commonly self-signed and
            # valid only for localhost. Pin the reviewed local middleware key
            # before the API key crosses the socket.
            pin_started = self._monotonic()
            self._pin_verifier(websocket.socket, self.config.truenas_spki_sha256)
            self._trace("truenas_spki", pin_started, outcome="verified")
            auth_started = self._monotonic()
            login = self._call(
                websocket,
                1,
                "auth.login_ex",
                [
                    {
                        "mechanism": "API_KEY_PLAIN",
                        "username": self.config.truenas_username,
                        "api_key": api_key,
                        "login_options": {"user_info": False},
                    }
                ],
                deadline=deadline,
            )
            if type(login) is not dict or login.get("response_type") != "SUCCESS":
                raise ProtocolError("truenas_authentication_failed")
            self._trace(
                "truenas_authentication", auth_started, outcome="authenticated"
            )
        except Exception:
            websocket.close()
            raise
        return websocket

    def _invalidate_read_session(self, reason: str) -> None:
        websocket = self._read_websocket
        self._read_websocket = None
        self._read_request_id = 1
        if websocket is not None:
            with suppress(Exception):
                websocket.close()
        LOG.info(json.dumps({"event": "truenas_session_invalidated", "reason": reason}))

    def _next_read_request_id(self) -> int:
        self._read_request_id += 1
        return self._read_request_id

    def status(self) -> dict[str, object]:
        waiting = self._monotonic()
        deadline = waiting + self.config.timeout_seconds
        with self._status_lock:
            lock_wait_ms = round(
                max(0.0, self._monotonic() - waiting) * 1000, 3
            )
            now = self._monotonic()
            if (
                self._cached_status is not None
                and now - self._cached_status[0] <= self._STATUS_CACHE_SECONDS
            ):
                self._trace(
                    "truenas_status",
                    waiting,
                    outcome="cache_hit",
                    lock_wait_ms=lock_wait_ms,
                )
                return dict(self._cached_status[1])
            result = self._fresh_status(deadline=deadline)
            self._cached_status = (self._monotonic(), result)
            self._trace(
                "truenas_status",
                waiting,
                outcome="fresh",
                lock_wait_ms=lock_wait_ms,
            )
            return dict(result)

    def _fresh_status(self, *, deadline: float) -> dict[str, object]:
        reused_session = self._read_websocket is not None
        try:
            if self._read_websocket is None:
                self._read_websocket = self._session(
                    self._read_api_key, deadline=deadline
                )
                self._read_request_id = 1
            websocket = self._read_websocket
            state = self._call(
                websocket,
                self._next_read_request_id(),
                "system.state",
                [],
                deadline=deadline,
            )
            version = self._call(
                websocket,
                self._next_read_request_id(),
                "system.version_short",
                [],
                deadline=deadline,
            )
            info = self._call(
                websocket,
                self._next_read_request_id(),
                "system.info",
                [],
                deadline=deadline,
            )
        except ProtocolError as exc:
            self._invalidate_read_session(str(exc))
            raise
        except Exception:  # noqa: BLE001 - transport details can contain secrets/URLs.
            self._invalidate_read_session("transport_error")
            if reused_session:
                # A server-closed or otherwise ambiguous persistent session is
                # never reused. Retry once on a newly pinned/authenticated
                # stream; a fresh-connect failure remains a bounded failure.
                return self._fresh_status(deadline=deadline)
            raise ProtocolError("truenas_unavailable") from None
        if state not in {"BOOTING", "READY", "SHUTTING_DOWN"} or type(info) is not dict:
            raise ProtocolError("truenas_malformed_response")
        uptime = info.get("uptime_seconds")
        if type(uptime) not in (int, float) or not math.isfinite(uptime) or uptime < 0:
            uptime = None
        return {
            "reachable": True,
            "hostname": _bounded_text(info.get("hostname"), 255),
            "version": _bounded_text(version, 128),
            "uptime_seconds": uptime,
            "system_state": {
                "BOOTING": "unknown",
                "READY": "online",
                "SHUTTING_DOWN": "shutting_down",
            }[state],
        }

    def shutdown(self) -> dict[str, object]:
        if not self.config.shutdown_enabled:
            raise ProtocolError("operation_disabled")
        if self._shutdown_api_key is None:
            raise ProtocolError("shutdown_credential_unavailable")
        deadline = self._monotonic() + self.config.timeout_seconds
        try:
            with self._session(self._shutdown_api_key, deadline=deadline) as websocket:
                result = self._call(
                    websocket,
                    2,
                    self._SHUTDOWN_METHOD,
                    list(self._SHUTDOWN_PARAMS),
                    deadline=deadline,
                )
        except ProtocolError:
            raise
        except TimeoutError:
            raise ProtocolError("timeout") from None
        except Exception:  # noqa: BLE001 - transport details can expose the endpoint.
            raise ProtocolError("truenas_unavailable") from None
        if result is not None:
            raise ProtocolError("truenas_malformed_response")
        return {"accepted": True, "state": "scheduled", "method": "system.shutdown"}


@dataclass(slots=True)
class JellyfinBackend:
    config: AgentConfig
    opener: Callable[..., Any] = urlopen

    def status(self) -> dict[str, object]:
        request = Request(
            self.config.jellyfin_url.rstrip("/") + self.config.jellyfin_health_path,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            with self.opener(request, timeout=self.config.timeout_seconds) as response:
                status = int(getattr(response, "status", 200))
                raw = response.read(4096)
                version = response.headers.get("X-Application-Version")
        except HTTPError as exc:
            return {
                "reachable": True,
                "ready": False,
                "version": None,
                "http_status": exc.code,
            }
        except (URLError, TimeoutError, OSError, ValueError):
            return {
                "reachable": False,
                "ready": False,
                "version": None,
                "http_status": None,
            }
        # The body is deliberately not parsed or returned. Jellyfin health is
        # currently a readiness status code, not an arbitrary response proxy.
        del raw
        return {
            "reachable": True,
            "ready": status == 200,
            "version": _bounded_text(version, 64),
            "http_status": status if 100 <= status <= 599 else None,
        }


class LocalStatusBackend:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._started = monotonic()

    def status(self) -> dict[str, object]:
        return {
            "hostname": socket.gethostname()[:255] or None,
            "uptime_seconds": max(0.0, self._monotonic() - self._started),
        }
