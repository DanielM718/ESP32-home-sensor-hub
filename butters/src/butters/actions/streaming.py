"""Bounded Butters-owned workflow; no WOL or service-control implementation here."""

import secrets
import threading
import time

from butters.actions.broker import BrokerClient, BrokerOperation
from butters.skills.model import SkillError


class StreamingWorkflow:
    STEPS = ("host", "ssh", "agent", "session", "parsec_service", "parsec_app", "verify")

    def __init__(self, compute, agent, broker_settings):
        self.compute, self.agent = compute, agent
        self.broker = BrokerClient(broker_settings)
        self.gate = threading.Lock()

    def broker_action(self, operation, cancel=None):
        result = self.broker.request(operation, request_id=secrets.token_urlsafe(18),
                                     cancel_event=cancel)
        if not result.ok:
            raise SkillError("broker_unavailable", "Streaming broker operation is unavailable")
        return dict(result.status)

    def status(self):
        agent = self.agent.status()
        service = self.broker_action(BrokerOperation.DESKTOP_PARSEC_STATUS)
        app = self.agent.invoke("desktop.app.status", {"app": "parsec"})
        ready = (agent["capabilities"]["gui_launch"] and service.get("plausibly_ready") is True
                 and app.get("running") is True)
        return {"success": True, "streaming_ready": bool(ready), "agent": agent,
                "parsec_service": service, "parsec_app": app,
                "note": "Host preparation only; an actual remote stream is not verified"}

    def prepare(self, cancel=None):
        if not self.gate.acquire(blocking=False):
            raise SkillError("busy", "A streaming preparation is already running")
        started = time.time()
        completed = []
        try:
            def checkpoint(step):
                if cancel is not None and cancel.is_set():
                    raise SkillError("cancelled", "Streaming cancelled after: " + ", ".join(completed))
                if time.time() - started > 260:
                    raise SkillError("timeout", "Streaming timed out at " + step)

            def wait(step, predicate, seconds):
                end = min(time.time() + seconds, started + 260)
                while time.time() < end:
                    checkpoint(step)
                    if predicate():
                        completed.append(step)
                        return
                    if cancel is not None:
                        cancel.wait(2)
                    else:
                        time.sleep(2)
                raise SkillError("precondition_failed", "Streaming unavailable at " + step)

            state = self.compute.execute("desktop.status")
            if not state.get("online"):
                checkpoint("wake")
                self.broker_action(BrokerOperation.DESKTOP_WAKE, cancel)
                completed.append("WOL packet sent")
            wait("host", lambda: self.compute.execute("desktop.status").get("online"), 90)
            wait("ssh", lambda: self.compute.execute("desktop.ssh_test").get("success"), 90)
            wait("agent", lambda: self.agent.status()["agent_connected"], 60)
            checkpoint("session")
            if not self.agent.status()["capabilities"]["gui_launch"]:
                raise SkillError("session_inactive", "Log in and unlock Windows; SSH remains available")
            completed.append("session")
            checkpoint("parsec_service")
            self.broker_action(BrokerOperation.DESKTOP_PARSEC_ENSURE, cancel)
            completed.append("parsec_service")
            checkpoint("parsec_app")
            app = self.agent.invoke("desktop.app.launch", {"app": "parsec"}, cancel=cancel)
            if not app.get("success"):
                raise SkillError(app.get("error", "launch_failed"), "Parsec interactive launch failed")
            completed.append("parsec_app")
            checkpoint("verify")
            state = self.status()
            if not state["streaming_ready"]:
                raise SkillError("not_ready", "Parsec host preparation could not be verified")
            completed.append("verify")
            return {**state, "action": "desktop.streaming.prepare", "steps": completed,
                    "started_at": started, "completed_at": time.time(),
                    "duration_seconds": time.time() - started, "target_host": "desktop",
                    "exit_code": 0, "stdout": "", "stderr": ""}
        finally:
            self.gate.release()
