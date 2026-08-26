"""Bounded warm lifecycle for expensive stateful streaming recognizers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from butters.stt.model import StreamingSTTEngine

LOGGER = logging.getLogger("butters.web.stt_pool")


class STTEnginePoolError(RuntimeError):
    code = "stt_unavailable"


@dataclass(frozen=True, slots=True)
class STTEngineLease:
    engine: StreamingSTTEngine
    reused: bool
    acquire_seconds: float
    initialization_seconds: float


class STTEnginePool:
    """Keep a small number of recognizers warm and never share one concurrently."""

    def __init__(
        self,
        factory: Callable[[], StreamingSTTEngine],
        *,
        max_size: int = 1,
        acquire_timeout_seconds: float = 10.0,
    ) -> None:
        if max_size < 1:
            raise ValueError("STT pool size must be positive")
        if acquire_timeout_seconds <= 0:
            raise ValueError("STT pool acquire timeout must be positive")
        self.factory = factory
        self.max_size = max_size
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self._condition = threading.Condition()
        self._available: list[StreamingSTTEngine] = []
        self._leased: set[int] = set()
        self._created = 0
        self._closed = False
        self._cold_loads = 0
        self._reuses = 0
        self._last_initialization_seconds = 0.0
        self._last_error: str | None = None

    def warm(self) -> STTEngineLease:
        lease = self.acquire()
        self.release(lease.engine)
        return lease

    def acquire(self) -> STTEngineLease:
        started = time.perf_counter()
        create = False
        deadline = time.monotonic() + self.acquire_timeout_seconds
        with self._condition:
            while True:
                if self._closed:
                    raise STTEnginePoolError("STT engine pool is closed")
                if self._available:
                    engine = self._available.pop()
                    self._leased.add(id(engine))
                    self._reuses += 1
                    return STTEngineLease(
                        engine,
                        True,
                        time.perf_counter() - started,
                        float(getattr(engine, "initialization_seconds", 0.0)),
                    )
                if self._created < self.max_size:
                    self._created += 1
                    create = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise STTEnginePoolError("timed out waiting for a warm STT engine")
                self._condition.wait(remaining)

        assert create
        try:
            engine = self.factory()
        except Exception as exc:
            with self._condition:
                self._created -= 1
                self._last_error = type(exc).__name__
                self._condition.notify()
            raise STTEnginePoolError(
                "local speech recognition is unavailable"
            ) from exc
        initialization = float(getattr(engine, "initialization_seconds", 0.0))
        with self._condition:
            if self._closed:
                self._created -= 1
                close = True
            else:
                self._leased.add(id(engine))
                self._cold_loads += 1
                self._last_initialization_seconds = initialization
                self._last_error = None
                close = False
        if close:
            engine.close()
            raise STTEnginePoolError("STT engine pool closed during initialization")
        return STTEngineLease(
            engine,
            False,
            time.perf_counter() - started,
            initialization,
        )

    def release(
        self, engine: StreamingSTTEngine, reusable: bool = True
    ) -> None:
        identity = id(engine)
        with self._condition:
            if identity not in self._leased:
                return
            self._leased.remove(identity)
        if reusable:
            try:
                engine.reset()
            except Exception:  # noqa: BLE001 - arbitrary recognizer is discarded
                reusable = False
        with self._condition:
            if reusable and not self._closed:
                self._available.append(engine)
                close = False
            else:
                self._created -= 1
                close = True
            self._condition.notify()
        if close:
            try:
                engine.close()
            except Exception:  # Teardown cannot recover this engine.
                LOGGER.warning("failed to close discarded STT engine", exc_info=True)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            engines = tuple(self._available)
            self._created -= len(engines)
            self._available.clear()
            self._condition.notify_all()
        for engine in engines:
            try:
                engine.close()
            except Exception:  # Best-effort process teardown.
                LOGGER.warning("failed to close pooled STT engine", exc_info=True)

    def stats(self) -> dict[str, object]:
        with self._condition:
            return {
                "max_size": self.max_size,
                "created": self._created,
                "available": len(self._available),
                "in_use": len(self._leased),
                "cold_loads": self._cold_loads,
                "reuses": self._reuses,
                "last_initialization_ms": round(
                    self._last_initialization_seconds * 1000, 3
                ),
                "last_error": self._last_error,
                "closed": self._closed,
            }
