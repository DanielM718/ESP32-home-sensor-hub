"""Live DesktopAgent adapter. Authorization remains in SkillRegistry/Coordinator."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import hmac
import json
import secrets
import threading
import time
import tomllib
import uuid
from pathlib import Path

from butters_agent.protocol import (SCHEMAS, ProtocolError, canonical, decode,
                                   envelope, parameters, sign, verify)
from butters.diagnostics.sanitizer import sanitize_value


class AgentHub:
    def __init__(self, config_path: Path):
        self.config = {}
        self.reason = "agent_not_configured"
        try:
            if config_path.stat().st_mode & 0o022:
                raise ValueError()
            config = tomllib.loads(config_path.read_text())
            key_path = Path(config["command_key_file"])
            if key_path.stat().st_mode & 0o027:
                raise ValueError()
            self.key = bytes.fromhex(key_path.read_text().strip())
            if (config["schema_version"] != 1 or len(self.key) != 32
                    or len(config["token_sha256"]) != 64):
                raise ValueError()
            self.config = config
            self.reason = "agent_disconnected"
        except (OSError, ValueError, KeyError):
            pass
        self.ws = None
        self.loop = None
        self.connection_id = None
        self.connected_at = None
        self.last_seen = 0
        self.session = {}
        self.version = None
        self.actions = []
        self.pending = {}
        self.reconnect_count = 0
        self.gate = threading.Lock()

    def status(self):
        age = time.monotonic() - self.last_seen
        connected = self.ws is not None and age < 45
        gui = connected and age < 30 and self.session.get("gui_launch") is True
        return {"available": connected, "agent_connected": connected,
            "reason": None if connected else self.reason,
            "connected_since": self.connected_at, "last_heartbeat_age_seconds":
            round(age, 2) if self.last_seen else None, "version": self.version,
            "protocol": 1, "reconnect_count": self.reconnect_count,
            "session": self.session if connected else {"state": "UNKNOWN"},
            "interactive_session": connected and self.session.get("interactive_session") is True,
            "capabilities": {"gui_launch": gui, "vm_control": False,
                             "streaming_ready": False},
            "actions": self.actions if connected else [], "observed_at": time.time(),
            "confidence": "observed" if connected else "unknown"}

    def snapshot(self):
        return self.status()

    def run_registered(self, application):
        return self.invoke("desktop.app.launch", {"app": application})

    def invoke(self, action, values, *, cancel=None, request_id=None, idempotency_key=None):
        parameters(action, values)
        if not self.status()["agent_connected"] or self.loop is None:
            return self.failure(action, "agent_unavailable")
        if action not in self.actions:
            return self.failure(action, "unsupported_action")
        if not self.gate.acquire(blocking=False):
            return self.failure(action, "busy")
        try:
            future = asyncio.run_coroutine_threadsafe(self._invoke(
                action, values, cancel, request_id, idempotency_key), self.loop)
            try:
                return future.result(timeout=35)
            except concurrent.futures.TimeoutError:
                future.cancel()
                return self.failure(action, "timeout")
        finally:
            self.gate.release()

    @staticmethod
    def failure(action, code):
        return {"action": action, "success": False, "error": code,
                "target_host": "desktop", "exit_code": 1, "stdout": "", "stderr": ""}

    async def _invoke(self, action, values, cancel, request_id, idempotency_key):
        rid = request_id or str(uuid.uuid4())
        ws, cid = self.ws, self.connection_id
        ack, result = asyncio.Event(), asyncio.get_running_loop().create_future()
        self.pending[rid] = (ack, result, action)
        frame = envelope("request", cid, request_id=rid, action=action,
            target=self.config["agent_id"], parameters=values,
            idempotency_key=idempotency_key or str(uuid.uuid4()), timeout_seconds=30)
        try:
            await ws.send_text(canonical(sign(frame, self.key)).decode())
            await asyncio.wait_for(ack.wait(), 3)
            deadline = time.monotonic() + 30
            while not result.done():
                if cancel is not None and cancel.is_set():
                    raise ProtocolError("cancelled")
                if time.monotonic() > deadline:
                    raise ProtocolError("timeout")
                await asyncio.sleep(.1)
            value = result.result()
            return {**value, "request_id": rid, "transport": "desktop_agent_wss",
                    "agent_version": self.version}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, ProtocolError) else "transport_error"
            return self.failure(action, code)
        finally:
            self.pending.pop(rid, None)
            if not result.done() and ws is self.ws:
                try:
                    await ws.send_text(canonical(sign(envelope("cancel", cid,
                        request_id=rid), self.key)).decode())
                except Exception:
                    pass

    async def socket(self, ws):
        # Agent credentials never authorize browser routes, and Origin-bearing browser
        # sockets may not use this machine endpoint. Existing browser gates are untouched.
        if not self.config or ws.headers.get("origin") is not None:
            await ws.close(code=1008)
            return
        await ws.accept()
        current = False
        try:
            hello = decode(await asyncio.wait_for(ws.receive_text(), 5))
            token = hello.get("token")
            if (set(hello) != {"type", "protocol", "schema", "agent_id", "version", "token", "actions"}
                    or hello["type"] != "hello" or hello["protocol"] != 1 or hello["schema"] != 1
                    or type(hello["protocol"]) is not int or type(hello["schema"]) is not int
                    or hello["agent_id"] != self.config["agent_id"]
                    or not isinstance(token, str) or len(token) != 64
                    or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),
                                               self.config["token_sha256"])
                    or type(hello["actions"]) is not list
                    or any(not isinstance(a, str) or a not in SCHEMAS for a in hello["actions"])
                    or not isinstance(hello["version"], str) or len(hello["version"]) > 32):
                raise ProtocolError("unauthorized")
            old = self.ws
            if old is not None:
                self._disconnect()
                await old.close(code=1012)
            self.ws = ws
            self.loop = asyncio.get_running_loop()
            self.connection_id = secrets.token_hex(32)
            self.connected_at = time.time()
            self.last_seen = 0  # Connected is not READY until authenticated heartbeat.
            self.actions = hello["actions"]
            self.version = hello["version"]
            self.reconnect_count += 1
            current = True
            cid = self.connection_id
            await ws.send_text(canonical(sign(envelope("welcome", cid, protocol=1,
                server_time=time.time()), self.key)).decode())
            seq = -1
            while self.ws is ws:
                frame = verify(decode(await asyncio.wait_for(ws.receive_text(), 45)), self.key, cid)
                kind = frame.get("type")
                if kind == "heartbeat":
                    if (type(frame.get("seq")) is not int or frame["seq"] <= seq
                            or type(frame.get("session")) is not dict):
                        raise ProtocolError("malformed_message")
                    state = frame["session"]
                    if (state.get("state") not in {"ACTIVE", "LOCKED", "NONE", "MULTIPLE"}
                            or type(state.get("gui_launch")) is not bool
                            or type(state.get("interactive_session")) is not bool):
                        raise ProtocolError("malformed_message")
                    self.session = {k: v for k, v in state.items() if k in {
                        "state", "gui_launch", "interactive_session", "session_id", "observed_at"}}
                    self.last_seen = time.monotonic()
                    seq = frame["seq"]
                elif kind in {"ack", "result", "error"}:
                    pending = self.pending.get(frame.get("request_id"))
                    if kind == "error" and pending is None and len(self.pending) == 1:
                        pending = next(iter(self.pending.values()))
                    if pending:
                        ack, future, action = pending
                        if kind == "ack":
                            ack.set()
                        elif kind == "result":
                            value = frame.get("result")
                            if (not ack.is_set() or type(value) is not dict
                                    or type(value.get("success")) is not bool
                                    or value.get("action") != action):
                                raise ProtocolError("malformed_result")
                            if not future.done():
                                future.set_result(sanitize_value(value)[0])
                        else:
                            ack.set()
                            if not future.done():
                                future.set_result(self.failure(action, "agent_rejected_request"))
                else:
                    raise ProtocolError("malformed_message")
        except Exception as exc:
            # Only enumerated protocol codes or exception class names, never frames.
            self.reason = str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__
        finally:
            if current and self.ws is ws:
                self._disconnect()
            try:
                await ws.close(code=1008)
            except Exception:
                pass

    def _disconnect(self):
        self.ws = None
        self.reason = "agent_disconnected"
        for ack, future, action in self.pending.values():
            ack.set()
            if not future.done():
                future.set_result(self.failure(action, "transport_error"))
