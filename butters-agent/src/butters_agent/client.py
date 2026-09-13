"""Outbound-only pinned WSS client. Authentication follows TLS pin verification."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import ssl
import threading
import time
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from websockets.asyncio.client import connect

from . import AGENT_VERSION
from .profile import staging_fault_delays
from .protocol import (
    MAX_FRAME,
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

LOG = logging.getLogger("butters_agent")


def verify_pin(ssl_object, expected):
    certificate = x509.load_der_x509_certificate(
        ssl_object.getpeercert(binary_form=True)
    )
    public_key = certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    actual = hashlib.sha256(public_key).hexdigest()
    if not __import__("hmac").compare_digest(actual, expected):
        raise ProtocolError("server_identity_mismatch")


class Client:
    def __init__(self, config, credentials, engine):
        self.config = config
        self.credentials = credentials
        self.engine = engine
        self.key = bytes.fromhex(credentials["command_key"])
        self.cache = ReplayCache()
        self.connection_count = 0
        self.ack_delay, self.result_delay = staging_fault_delays(config)

    async def run(self):
        delay = 1
        while True:
            try:
                await self.session()
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - all network failures are redacted.
                # Never format exceptions: network exceptions may contain headers/tokens.
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

    async def session(self):
        url = self.config["url"]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "wss"
            or parsed.path != "/agent/v1/session"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ProtocolError("invalid_configuration")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # SPKI pin is the server identity, below.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        async with connect(
            url,
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
            verify_pin(
                websocket.transport.get_extra_info("ssl_object"),
                self.config["spki_sha256"],
            )
            # No Authorization header: token is first sent AFTER pin verification.
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": 1,
                        "schema": 1,
                        "agent_id": self.config["agent_id"],
                        "version": AGENT_VERSION,
                        "token": self.credentials["token"],
                        "actions": sorted(SCHEMAS),
                    }
                )
            )
            welcome = decode(await asyncio.wait_for(websocket.recv(), 5))
            if welcome.get("type") != "welcome":
                raise ProtocolError("unauthorized")
            connection_id = welcome.get("connection_id")
            verify(welcome, self.key, connection_id)
            if welcome.get("protocol") != 1:
                raise ProtocolError("protocol_mismatch")
            if abs(time.time() - welcome["server_time"]) > 30:
                raise ProtocolError("clock_skew")
            self.connection_count += 1
            LOG.info(
                json.dumps(
                    {
                        "event": "connected",
                        "count": self.connection_count,
                    }
                )
            )
            await self._connected(websocket, connection_id)

    async def _connected(self, websocket, connection_id):
        active = {}

        async def send(kind, **fields):
            frame = sign(envelope(kind, connection_id, **fields), self.key)
            await websocket.send(canonical(frame).decode())

        async def heartbeat():
            sequence = 0
            while True:
                state = await asyncio.to_thread(self.engine.platform.session)
                await send(
                    "heartbeat",
                    seq=sequence,
                    session=state,
                    version=AGENT_VERSION,
                )
                sequence += 1
                await asyncio.sleep(15)

        async def execute(frame, cancel):
            request_id = frame["request_id"]

            async def expire():
                delay = max(
                    0,
                    frame["issued_at"] + frame["timeout_seconds"] - time.time(),
                )
                await asyncio.sleep(delay)
                cancel.set()

            deadline = asyncio.create_task(expire())
            try:
                result = self.cache.get(frame)
                duplicate = result is not None
                if result is None:
                    result = await asyncio.to_thread(
                        self.engine.invoke,
                        frame["action"],
                        frame["parameters"],
                        cancel,
                    )
                    self.cache.put(frame, result)
                if self.result_delay:
                    await asyncio.sleep(self.result_delay)
                await send(
                    "result",
                    request_id=request_id,
                    result=result,
                    duplicate=duplicate,
                )
                LOG.info(
                    json.dumps(
                        {
                            "event": "result",
                            "request_id": request_id,
                            "action": frame["action"],
                            "success": result["success"],
                            "duplicate": duplicate,
                        }
                    )
                )
            finally:
                deadline.cancel()
                active.pop(request_id, None)

        pulse = asyncio.create_task(heartbeat())
        tasks = set()
        try:
            async for raw in websocket:
                request_identity = None
                try:
                    frame = verify(decode(raw), self.key, connection_id)
                    # Preserve an explicit, validated identity even when another
                    # request field is rejected. A future ingress must never infer
                    # request ownership from the number of pending operations.
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
                        validate_request_id(frame["request_id"])
                        pending = active.get(frame["request_id"])
                        if pending:
                            pending[1].set()
                        continue
                    request(frame, target=self.config["agent_id"])
                    request_id = frame["request_id"]
                    self.cache.get(frame)  # Detect conflicts before acknowledgement.
                    existing = active.get(request_id)
                    if existing:
                        if canonical(existing[0]) != canonical(frame):
                            raise ProtocolError("duplicate_request")
                        await send("ack", request_id=request_id)
                        continue
                    # One live operation: no queued side effects or racing keys.
                    if active:
                        raise ProtocolError("busy")
                    if self.ack_delay:
                        await asyncio.sleep(self.ack_delay)
                    await send("ack", request_id=request_id)
                    cancel = threading.Event()
                    active[request_id] = (frame, cancel)
                    task = asyncio.create_task(execute(frame, cancel))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                except ProtocolError as exc:
                    fields = {"error": str(exc)}
                    if request_identity is not None:
                        fields["request_id"] = request_identity
                    await send("error", **fields)
        finally:
            pulse.cancel()
            for _, cancel in active.values():
                cancel.set()
            # Do not abandon a worker and reconnect while it can still launch a process.
            await asyncio.gather(pulse, *tasks, return_exceptions=True)
