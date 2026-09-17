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
from typing import Any, ClassVar
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
    _NETWORK_METHOD = "reporting.netdata_get_data"
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
            self._NETWORK_METHOD,
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

    def _session(self, api_key: str, username: str, *, deadline: float) -> Any:
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
                        "username": username,
                        "api_key": api_key,
                        "login_options": {"user_info": False},
                    }
                ],
                deadline=deadline,
            )
            if type(login) is not dict or login.get("response_type") != "SUCCESS":
                raise ProtocolError("truenas_authentication_failed")
            self._trace("truenas_authentication", auth_started, outcome="authenticated")
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

    def _read_call(
        self, method: str, params: list[object], *, deadline: float
    ) -> object:
        reused_session = self._read_websocket is not None
        try:
            if self._read_websocket is None:
                self._read_websocket = self._session(
                    self._read_api_key,
                    self.config.truenas_username,
                    deadline=deadline,
                )
                self._read_request_id = 1
            return self._call(
                self._read_websocket,
                self._next_read_request_id(),
                method,
                params,
                deadline=deadline,
            )
        except ProtocolError as exc:
            self._invalidate_read_session(str(exc))
            raise
        except Exception:  # noqa: BLE001 - transport details may contain endpoint data.
            self._invalidate_read_session("transport_error")
            if reused_session:
                return self._read_call(method, params, deadline=deadline)
            raise ProtocolError("truenas_unavailable") from None

    def status(self) -> dict[str, object]:
        waiting = self._monotonic()
        deadline = waiting + self.config.timeout_seconds
        with self._status_lock:
            lock_wait_ms = round(max(0.0, self._monotonic() - waiting) * 1000, 3)
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
        state = self._read_call("system.state", [], deadline=deadline)
        version = self._read_call("system.version_short", [], deadline=deadline)
        info = self._read_call("system.info", [], deadline=deadline)
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

    def interface_rate(self) -> dict[str, object]:
        """Read one operator-owned physical interface from supported reporting.

        TrueNAS reports interface graph values as ``[time, received, sent]``
        rows whose columns are named by ``legend``.  The graph's native unit is
        kilobits/second, so no counter differencing is appropriate here.
        """

        if not self.config.network_monitoring_enabled:
            return {
                "available": False,
                "tx_mbps": None,
                "rx_mbps": None,
                "sample_window_seconds": None,
                "reason": "monitoring_disabled",
            }
        deadline = self._monotonic() + self.config.timeout_seconds
        end = int(time.time())
        start = max(1, end - 10)
        with self._status_lock:
            value = self._read_call(
                self._NETWORK_METHOD,
                [
                    [
                        {
                            "name": "interface",
                            "identifier": self.config.physical_interface,
                        }
                    ],
                    {"aggregate": False, "start": start, "end": end},
                ],
                deadline=deadline,
            )
        try:
            graph = value[0]
            if (
                type(value) is not list
                or len(value) != 1
                or type(graph) is not dict
                or graph.get("name") != "interface"
                or graph.get("identifier") != self.config.physical_interface
                or type(graph.get("data")) is not list
            ):
                raise ValueError
            legend = graph.get("legend")
            if (
                type(legend) is not list
                or any(type(item) is not str for item in legend)
                or not {"time", "received", "sent"}.issubset(legend)
            ):
                raise ValueError
            points = [
                item
                for item in graph["data"]
                if type(item) is list and len(item) == len(legend)
            ]
            point = points[-1]
            time_column = legend.index("time")
            received_column = legend.index("received")
            sent_column = legend.index("sent")
            sent = point[sent_column]
            received = point[received_column]
            if type(sent) not in (int, float) or type(received) not in (int, float):
                raise ValueError
            sent = float(sent)
            received = float(received)
            if not all(math.isfinite(item) and item >= 0 for item in (sent, received)):
                raise ValueError
            # InterfacePlugin.vertical_label is "Kilobits/s" in the TrueNAS
            # reporting implementation. Convert decimal kbit/s to Mbit/s.
            sample_window = None
            if len(points) > 1:
                first_time = points[-2][time_column]
                last_time = point[time_column]
                if type(first_time) in (int, float) and type(last_time) in (int, float):
                    sample_window = max(0.0, float(last_time) - float(first_time))
        except (IndexError, KeyError, TypeError, ValueError):
            raise ProtocolError("truenas_malformed_response") from None
        return {
            "available": True,
            "tx_mbps": round(sent / 1000, 3),
            "rx_mbps": round(received / 1000, 3),
            "sample_window_seconds": sample_window,
            "reason": None,
        }

    def shutdown(self) -> dict[str, object]:
        if not self.config.shutdown_enabled:
            raise ProtocolError("operation_disabled")
        if self._shutdown_api_key is None:
            raise ProtocolError("shutdown_credential_unavailable")
        deadline = self._monotonic() + self.config.timeout_seconds
        try:
            assert self.config.truenas_shutdown_username is not None
            with self._session(
                self._shutdown_api_key,
                self.config.truenas_shutdown_username,
                deadline=deadline,
            ) as websocket:
                # A JSON-RPC success envelope is the acceptance boundary. Some
                # TrueNAS 25.10 builds return null as documented while the
                # production appliance returned a non-null acknowledgement.
                # The value is deliberately discarded: it cannot influence or
                # broaden this fixed zero-argument operation.
                self._call(
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
        return {"accepted": True, "state": "scheduled", "method": "system.shutdown"}


@dataclass(slots=True)
class JellyfinBackend:
    config: AgentConfig
    api_key: str | None = None
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

    def sessions(self) -> dict[str, object]:
        if not self.config.jellyfin_session_monitoring_enabled:
            return {"available": False, "reason": "monitoring_disabled", "sessions": []}
        if self.api_key is None:
            return {
                "available": False,
                "reason": "jellyfin_credential_unavailable",
                "sessions": [],
            }
        request = Request(
            self.config.jellyfin_url.rstrip("/") + "/Sessions?activeWithinSeconds=90",
            method="GET",
            headers={
                "Accept": "application/json",
                "X-Emby-Token": self.api_key,
            },
        )
        try:
            with self.opener(request, timeout=self.config.timeout_seconds) as response:
                if int(getattr(response, "status", 200)) != 200:
                    raise ProtocolError("jellyfin_unavailable")
                raw = response.read(256 * 1024 + 1)
        except HTTPError as exc:
            raise ProtocolError(
                "jellyfin_authentication_failed"
                if exc.code in {401, 403}
                else "jellyfin_unavailable"
            ) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ProtocolError("jellyfin_unavailable") from None
        if len(raw) > 256 * 1024:
            raise ProtocolError("jellyfin_malformed_response")
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise ProtocolError("jellyfin_malformed_response") from None
        if type(value) is not list:
            raise ProtocolError("jellyfin_malformed_response")
        projected = []
        for item in value[:64]:
            session = self._session(item)
            if session is not None:
                projected.append(session)
            if len(projected) >= 32:
                break
        return {"available": True, "reason": None, "sessions": projected}

    def _session(self, value: object) -> dict[str, object] | None:
        if type(value) is not dict or value.get("NowPlayingItem") is None:
            return None
        play = value.get("PlayState")
        now_playing = value.get("NowPlayingItem")
        if type(play) is not dict or type(now_playing) is not dict:
            return None
        identifier = _bounded_text(value.get("Id"), 64)
        if identifier is None:
            return None
        method = {
            "DirectPlay": "direct_play",
            "DirectStream": "direct_stream",
            "Transcode": "transcode",
        }.get(play.get("PlayMethod"), "unknown")
        transcode = value.get("TranscodingInfo")
        bitrate = None
        bitrate_source = "unavailable"
        if type(transcode) is dict and type(transcode.get("Bitrate")) in (int, float):
            bitrate = float(transcode["Bitrate"])
            bitrate_source = "transcode_reported"
        elif type(now_playing.get("Bitrate")) in (int, float):
            bitrate = float(now_playing["Bitrate"])
            bitrate_source = "source_reported"
        if bitrate is not None and (not math.isfinite(bitrate) or bitrate < 0):
            bitrate = None
            bitrate_source = "unavailable"
        endpoint = self._endpoint_ip(value.get("RemoteEndPoint"))
        classification = self._classify(endpoint)
        name = _bounded_text(now_playing.get("Name"), 128)
        series = _bounded_text(now_playing.get("SeriesName"), 128)
        label = f"{series} — {name}" if series and name else name or series
        return {
            "session_id": identifier,
            "user": _bounded_text(value.get("UserName"), 80),
            "playing": value.get("IsActive") is not False,
            "paused": play.get("IsPaused") is True,
            "classification": classification,
            "play_method": method,
            "observed_mbps": None if bitrate is None else round(bitrate / 1_000_000, 3),
            "bitrate_source": bitrate_source,
            "position_ticks": play.get("PositionTicks")
            if type(play.get("PositionTicks")) is int and play["PositionTicks"] >= 0
            else None,
            "client": _bounded_text(value.get("Client"), 64),
            "device": _bounded_text(value.get("DeviceName"), 80),
            "item": label,
        }

    @staticmethod
    def _endpoint_ip(
        value: object,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        if not isinstance(value, str) or len(value) > 128:
            return None
        candidate = value.strip()
        if candidate.startswith("[") and "]" in candidate:
            candidate = candidate[1 : candidate.index("]")]
        else:
            try:
                return ipaddress.ip_address(candidate)
            except ValueError:
                if candidate.count(":") == 1:
                    candidate = candidate.rsplit(":", 1)[0]
        try:
            return ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            return None

    def _classify(self, endpoint: ipaddress._BaseAddress | None) -> str:
        if endpoint is None:
            return "unknown"
        try:
            if any(
                endpoint in ipaddress.ip_network(item)
                for item in self.config.local_networks
            ):
                return "local"
            if any(
                endpoint in ipaddress.ip_network(item)
                for item in self.config.remote_networks
            ):
                return "remote"
        except TypeError:
            return "unknown"
        return "unknown"


class TailscaleMetricsBackend:
    """Read only the documented Tailscale throughput counters."""

    _NAMES: ClassVar[dict[str, str]] = {
        "tailscaled_outbound_bytes_total": "tx",
        "tailscaled_inbound_bytes_total": "rx",
    }

    def __init__(
        self, config: AgentConfig, opener: Callable[..., Any] = urlopen
    ) -> None:
        self.config = config
        self.opener = opener

    def counters(self) -> dict[str, object]:
        request = Request(
            self.config.tailscale_metrics_url,
            method="GET",
            headers={"Accept": "text/plain"},
        )
        try:
            with self.opener(request, timeout=self.config.timeout_seconds) as response:
                if int(getattr(response, "status", 200)) != 200:
                    raise ProtocolError("tailscale_metrics_unavailable")
                raw = response.read(256 * 1024 + 1)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise ProtocolError("tailscale_metrics_unavailable") from None
        if len(raw) > 256 * 1024:
            raise ProtocolError("tailscale_metrics_malformed")
        totals = {"tx": 0.0, "rx": 0.0}
        paths: dict[str, dict[str, float]] = {}
        found: set[str] = set()
        try:
            lines = raw.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise ProtocolError("tailscale_metrics_malformed") from None
        for line in lines:
            if not line or line.startswith("#"):
                continue
            name = line.split("{", 1)[0].split(None, 1)[0]
            direction = self._NAMES.get(name)
            if direction is None:
                continue
            try:
                metric, raw_value = line.rsplit(None, 1)
                amount = float(raw_value)
            except (ValueError, TypeError):
                raise ProtocolError("tailscale_metrics_malformed") from None
            if not math.isfinite(amount) or amount < 0:
                raise ProtocolError("tailscale_metrics_malformed")
            path = "unknown"
            marker = 'path="'
            if marker in metric:
                path = metric.split(marker, 1)[1].split('"', 1)[0]
            if path not in {
                "direct_ipv4",
                "direct_ipv6",
                "derp",
                "peer_relay_ipv4",
                "peer_relay_ipv6",
            }:
                path = "unknown"
            totals[direction] += amount
            paths.setdefault(path, {"tx_bytes": 0.0, "rx_bytes": 0.0})[
                f"{direction}_bytes"
            ] += amount
            found.add(direction)
        if found != {"tx", "rx"}:
            raise ProtocolError("tailscale_metrics_malformed")
        bounded_paths = {
            key: {name: round(value) for name, value in counters.items()}
            for key, counters in sorted(paths.items())
        }
        return {
            "tx_bytes": totals["tx"],
            "rx_bytes": totals["rx"],
            "paths": bounded_paths,
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
