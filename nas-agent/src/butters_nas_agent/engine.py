"""Registry-only NAS operations with safe, action-specific results."""

from __future__ import annotations

import threading
import time

from . import AGENT_VERSION
from .protocol import ACTION_SCHEMA_VERSION, PROTOCOL_VERSION, ProtocolError, parameters


class Engine:
    def __init__(self, local, truenas, jellyfin, *, monotonic=time.monotonic) -> None:
        self.local = local
        self.truenas = truenas
        self.jellyfin = jellyfin
        self._mutation_lock = threading.Lock()
        self._transport_lock = threading.Lock()
        self._monotonic = monotonic
        self._connected_at: float | None = None
        self._connection_count = 0
        self._last_heartbeat_sequence: int | None = None

    def connected(self, connection_count: int) -> None:
        with self._transport_lock:
            self._connected_at = self._monotonic()
            self._connection_count = connection_count
            self._last_heartbeat_sequence = None

    def heartbeat_sent(self, sequence: int) -> None:
        with self._transport_lock:
            self._last_heartbeat_sequence = sequence

    def _agent_status(self) -> dict[str, object]:
        with self._transport_lock:
            connected_at = self._connected_at
            count = self._connection_count
            sequence = self._last_heartbeat_sequence
        return {
            **self.local.status(),
            "connection_count": count,
            "connection_uptime_seconds": None
            if connected_at is None
            else max(0.0, self._monotonic() - connected_at),
            "last_heartbeat_sequence": sequence,
        }

    def heartbeat_state(self) -> dict[str, object]:
        system = self._safe_status(self.truenas)
        jellyfin = self._safe_status(self.jellyfin)
        return {"system": system, "jellyfin": jellyfin}

    @staticmethod
    def _safe_status(backend) -> dict[str, object]:
        try:
            return backend.status()
        except ProtocolError as exc:
            return {"reachable": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 - never expose backend exception text.
            return {"reachable": False, "error": "backend_unavailable"}

    def invoke(
        self, action: str, values: dict[str, object], cancel=None
    ) -> dict[str, object]:
        started = time.time()
        result: dict[str, object] = {
            "action": action,
            "target": "nas-primary",
            "started_at": started,
            "success": False,
        }
        try:
            parameters(action, values)
            if cancel is not None and cancel.is_set():
                raise ProtocolError("cancelled")
            if action == "nas.agent.status":
                result.update(
                    success=True,
                    agent_version=AGENT_VERSION,
                    protocol_version=PROTOCOL_VERSION,
                    schema_version=ACTION_SCHEMA_VERSION,
                    **self._agent_status(),
                )
            elif action == "nas.system.status":
                # An unavailable local service is a successful observation,
                # not a transport failure for the NAS Agent operation.
                result.update(success=True, **self._safe_status(self.truenas))
            elif action == "nas.jellyfin.status":
                result.update(success=True, **self._safe_status(self.jellyfin))
            elif action == "nas.system.shutdown":
                if not self._mutation_lock.acquire(blocking=False):
                    raise ProtocolError("busy")
                try:
                    result.update(success=True, **self.truenas.shutdown())
                finally:
                    self._mutation_lock.release()
            else:  # Defensive even though parameters() already rejects it.
                raise ProtocolError("invalid_action")
        except ProtocolError as exc:
            result.update(success=False, error=str(exc))
        except TimeoutError:
            result.update(success=False, error="timeout")
        except Exception:  # noqa: BLE001 - exception strings can leak secret material.
            result.update(success=False, error="backend_unavailable")
        result["completed_at"] = time.time()
        result["duration_seconds"] = max(0.0, float(result["completed_at"]) - started)
        return result
