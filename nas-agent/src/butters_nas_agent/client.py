"""Outbound-only NAS Agent client with pinned Butters server identity."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import ssl
import threading
import time

from websockets.asyncio.client import connect

from . import AGENT_VERSION
from .protocol import (
    ACTION_SCHEMA_VERSION,
    MAX_FRAME,
    PROTOCOL_VERSION,
    SCHEMAS,
    ProtocolError,
    ReplayCache,
    canonical,
    decode,
    envelope,
    request,
    sign,
    validate_request_id,
    verify,
)
from .tls import verify_spki

LOG = logging.getLogger("butters_nas_agent")


class Client:
    def __init__(self, config, credentials, engine) -> None:
        self.config = config
        self.credentials = credentials
        self.engine = engine
        self.key = bytes.fromhex(credentials["command_key"])
        self.cache = ReplayCache()
        self.connection_count = 0

    async def run(self) -> None:
        delay = 1
        while True:
            try:
                await self.session()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - all network details are redacted.
                code = (
                    str(exc)
                    if isinstance(exc, ProtocolError)
                    else "connection_unavailable"
                )
                LOG.warning(json.dumps({"event": "disconnected", "reason": code}))
                if code in {
                    "server_identity_mismatch",
                    "unauthorized",
                    "invalid_signature",
                }:
                    delay = 60
            await asyncio.sleep(delay + random.random())
            delay = min(60, delay * 2)

    async def session(self) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = (
            ssl.CERT_NONE
        )  # SPKI is verified before credentials leave.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        async with connect(
            self.config.url,
            ssl=context,
            proxy=None,
            open_timeout=10,
            max_size=MAX_FRAME,
            max_queue=8,
            ping_interval=15,
            ping_timeout=20,
            close_timeout=3,
            compression=None,
            user_agent_header=None,
        ) as websocket:
            verify_spki(
                websocket.transport.get_extra_info("ssl_object"),
                self.config.spki_sha256,
            )
            await websocket.send(
                canonical(
                    {
                        "type": "hello",
                        "protocol": PROTOCOL_VERSION,
                        "schema": ACTION_SCHEMA_VERSION,
                        "agent_id": self.config.agent_id,
                        "version": AGENT_VERSION,
                        "token": self.credentials["token"],
                        "actions": sorted(SCHEMAS),
                    }
                ).decode("ascii")
            )
            welcome = decode(await asyncio.wait_for(websocket.recv(), 5))
            if welcome.get("type") != "welcome":
                raise ProtocolError("unauthorized")
            connection_id = welcome.get("connection_id")
            if not isinstance(connection_id, str):
                raise ProtocolError("unauthorized")
            verify(welcome, self.key, connection_id)
            if welcome.get("protocol") != PROTOCOL_VERSION:
                raise ProtocolError("protocol_mismatch")
            server_time = welcome.get("server_time")
            if (
                type(server_time) not in (int, float)
                or abs(time.time() - server_time) > 30
            ):
                raise ProtocolError("clock_skew")
            self.connection_count += 1
            self.engine.connected(self.connection_count)
            LOG.info(json.dumps({"event": "connected", "count": self.connection_count}))
            await self._connected(websocket, connection_id)

    async def _connected(self, websocket, connection_id: str) -> None:
        active: dict[str, tuple[dict[str, object], threading.Event]] = {}

        async def send(kind: str, **fields: object) -> None:
            frame = sign(envelope(kind, connection_id, **fields), self.key)
            await websocket.send(canonical(frame).decode("ascii"))

        async def heartbeat() -> None:
            sequence = 0
            while True:
                started = time.monotonic()
                state = await asyncio.to_thread(self.engine.heartbeat_state)
                collected = time.monotonic()
                await send(
                    "heartbeat", seq=sequence, state=state, version=AGENT_VERSION
                )
                sent = time.monotonic()
                self.engine.heartbeat_sent(sequence)
                self.config.health_file.parent.mkdir(parents=True, exist_ok=True)
                self.config.health_file.touch(mode=0o600, exist_ok=True)
                LOG.info(
                    json.dumps(
                        {
                            "event": "heartbeat_sent",
                            "sequence": sequence,
                            "backend_ms": round((collected - started) * 1000, 3),
                            "send_ms": round((sent - collected) * 1000, 3),
                            "total_ms": round((sent - started) * 1000, 3),
                        },
                        sort_keys=True,
                    )
                )
                sequence += 1
                await asyncio.sleep(self.config.heartbeat_seconds)

        async def execute(frame: dict[str, object], cancel: threading.Event) -> None:
            request_id = str(frame["request_id"])
            action = str(frame["action"])
            started = time.monotonic()

            async def expire() -> None:
                issued_at = float(frame["issued_at"])
                timeout = int(frame["timeout_seconds"])
                await asyncio.sleep(max(0, issued_at + timeout - time.time()))
                cancel.set()

            deadline = asyncio.create_task(expire())
            try:
                result = self.cache.get(frame)
                duplicate = result is not None
                if result is None:
                    backend_started = time.monotonic()
                    result = await asyncio.to_thread(
                        self.engine.invoke,
                        frame["action"],
                        frame["parameters"],
                        cancel,
                    )
                    backend_ms = (time.monotonic() - backend_started) * 1000
                    self.cache.put(frame, result)
                else:
                    backend_ms = 0.0
                send_started = time.monotonic()
                await send(
                    "result", request_id=request_id, result=result, duplicate=duplicate
                )
                sent = time.monotonic()
                LOG.info(
                    json.dumps(
                        {
                            "event": "result",
                            "request_id": request_id,
                            "action": action,
                            "success": result["success"],
                            "duplicate": duplicate,
                            "backend_ms": round(backend_ms, 3),
                            "send_ms": round((sent - send_started) * 1000, 3),
                            "total_ms": round((sent - started) * 1000, 3),
                        }
                    )
                )
            finally:
                deadline.cancel()
                active.pop(request_id, None)

        pulse = asyncio.create_task(heartbeat())
        tasks: set[asyncio.Task] = set()
        try:
            async for raw in websocket:
                request_identity = None
                try:
                    frame = verify(decode(raw), self.key, connection_id)
                    try:
                        request_identity = validate_request_id(frame.get("request_id"))
                    except ProtocolError:
                        pass
                    if frame.get("type") == "cancel":
                        if set(frame) != {
                            "type",
                            "connection_id",
                            "issued_at",
                            "sig",
                            "request_id",
                        }:
                            raise ProtocolError("malformed_request")
                        request_id = validate_request_id(frame["request_id"])
                        pending = active.get(request_id)
                        if pending:
                            pending[1].set()
                        continue
                    request(frame, target=self.config.agent_id)
                    request_id = str(frame["request_id"])
                    LOG.info(
                        json.dumps(
                            {
                                "event": "request_received",
                                "request_id": request_id,
                                "action": frame["action"],
                            },
                            sort_keys=True,
                        )
                    )
                    self.cache.get(frame)
                    existing = active.get(request_id)
                    if existing:
                        if canonical(existing[0]) != canonical(frame):
                            raise ProtocolError("duplicate_request")
                        await send("ack", request_id=request_id)
                        continue
                    if active:
                        raise ProtocolError("busy")
                    await send("ack", request_id=request_id)
                    cancel = threading.Event()
                    active[request_id] = (frame, cancel)
                    task = asyncio.create_task(execute(frame, cancel))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                except ProtocolError as exc:
                    fields: dict[str, object] = {"error": str(exc)}
                    if request_identity is not None:
                        fields["request_id"] = request_identity
                    await send("error", **fields)
        finally:
            pulse.cancel()
            for _, cancel in active.values():
                cancel.set()
            await asyncio.gather(pulse, *tasks, return_exceptions=True)


def healthcheck(path, *, now: float | None = None, maximum_age: float = 90) -> bool:
    try:
        age = (time.time() if now is None else now) - os.stat(path).st_mtime
    except OSError:
        return False
    return 0 <= age <= maximum_age
