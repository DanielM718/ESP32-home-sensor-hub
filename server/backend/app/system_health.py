"""Bounded, read-only dependency health for the dashboard.

``/api/status`` already answers "is the unit running". That is not the same
question as "is this part of the system working": a unit can be ``active`` while
the thing it exists to do has stopped happening. This module answers the second
question by combining two independent signals per dependency:

``process``
    the state of one fixed, allow-listed systemd unit.

``data``
    the freshness of data that only exists if the dependency is actually
    working -- a recent sensor sample, a recent printer observation.

A dependency is only ``healthy`` when every signal it has is good, and the
``basis`` field always states which signals were available, so a
``process_only`` verdict is never mistaken for end-to-end verification.

Design constraints, all deliberate:

* No caller input reaches this module. Dependencies, units and thresholds are
  module constants; there is no unit, path, host or command parameter.
* No secret, token, URL, filesystem path or environment value is returned.
* Every collector runs under a hard timeout and the whole snapshot runs under a
  total budget. A dependency that is down or hanging must never turn this
  endpoint into an outage of its own -- reporting the outage is its entire job.
* The snapshot performs no network I/O of its own. It reads local systemd state
  and local resolvers the app already owns, so observing health cannot itself
  add load to a dependency that is already struggling.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("home_sensor.system_health")

HEALTHY = "healthy"
DEGRADED = "degraded"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

#: Worst-to-best, used to fold many dependency states into one overall state.
_SEVERITY = {UNAVAILABLE: 3, DEGRADED: 2, UNKNOWN: 1, HEALTHY: 0}

#: Per-collector wall-clock ceiling. Collectors are local reads; anything slower
#: than this is already a symptom, and the caller learns more from a prompt
#: ``unknown`` than from a hung request.
COLLECTOR_TIMEOUT_SECONDS = 3.0

#: Ceiling for the whole snapshot. Collectors run concurrently, so this is not
#: the sum of their individual budgets.
TOTAL_BUDGET_SECONDS = 6.0

SERVER_ROOT = Path(__file__).resolve().parents[2]

#: Written by the deployment procedure into the deployed tree. Production is an
#: rsync target rather than a checkout, so there is no git metadata there and a
#: stamp file is the only way to make deployed revision visible.
RELEASE_FILE = SERVER_ROOT / "RELEASE"


@dataclass(frozen=True, slots=True)
class DependencyDefinition:
    """One fixed dependency. Never constructed from request data."""

    dependency_id: str
    display_name: str
    #: Allow-listed systemd unit providing the process signal, if any.
    unit: str | None = None
    #: Which data collector supplies the data signal, if any.
    data_signal: str | None = None
    #: Core dependencies decide the overall state. Neighbours are reported but
    #: never make the sensor dashboard describe itself as broken.
    core: bool = True
    description: str = ""


DEPENDENCY_DEFINITIONS: tuple[DependencyDefinition, ...] = (
    DependencyDefinition(
        "dashboard",
        "Dashboard API",
        unit="home-sensor-dashboard.service",
        core=True,
        description="This service. It answered, so it is running.",
    ),
    DependencyDefinition(
        "mqtt_broker",
        "MQTT broker",
        unit="mosquitto.service",
        data_signal="sensor_freshness",
        core=True,
        description="Sensor nodes publish here.",
    ),
    DependencyDefinition(
        "sensor_ingest",
        "Sensor ingest",
        unit="home-sensor-bridge.service",
        data_signal="sensor_freshness",
        core=True,
        description="Moves MQTT sensor messages into InfluxDB.",
    ),
    DependencyDefinition(
        "influx",
        "InfluxDB",
        unit="influxdb.service",
        data_signal="influx_read",
        core=True,
        description="Time-series store behind every reading and history query.",
    ),
    DependencyDefinition(
        "printer_telemetry",
        "Printer telemetry",
        unit="home-sensor-printer-observer.service",
        data_signal="printer_freshness",
        core=True,
        description="Read-only X2D observer.",
    ),
    DependencyDefinition(
        "home_assistant",
        "Home Assistant",
        unit=None,
        data_signal="home_assistant_source",
        core=False,
        description=(
            "Upstream of printer telemetry. Observed indirectly through the "
            "freshness of what the observer received, so no Home Assistant "
            "credential is needed here."
        ),
    ),
    DependencyDefinition(
        "export_worker",
        "CSV export worker",
        unit="home-sensor-export-worker.service",
        core=False,
        description="Builds requested monitoring exports.",
    ),
    DependencyDefinition(
        "grafana",
        "Grafana",
        unit="grafana-server.service",
        core=False,
        description="Optional dashboards over the same InfluxDB data.",
    ),
    DependencyDefinition(
        "butters_web",
        "Butters web assistant",
        unit="butters-web.service",
        core=False,
        description="Neighbouring assistant service on the same Pi.",
    ),
    DependencyDefinition(
        "butters_action_broker",
        "Butters action broker",
        unit="butters-action-broker.socket",
        core=False,
        description=(
            "Privileged action broker's activation socket. Observed through "
            "systemd only: connecting to the socket would activate a root "
            "service, which observation must never do."
        ),
    ),
)


@dataclass
class _Signal:
    """One collected signal plus the reason it holds that value."""

    state: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


class SystemHealthProvider:
    """Assemble a bounded health snapshot from local signals only."""

    def __init__(
        self,
        *,
        status_provider: Any,
        latest_resolver: Callable[[], Any] | None = None,
        printer_resolver: Callable[[], Any] | None = None,
        node_stale_after_seconds: int = 1800,
        air_quality_stale_after_seconds: int = 20,
        printer_stale_after_seconds: int = 300,
        dependencies: Sequence[DependencyDefinition] = DEPENDENCY_DEFINITIONS,
        collector_timeout_seconds: float = COLLECTOR_TIMEOUT_SECONDS,
        total_budget_seconds: float = TOTAL_BUDGET_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        revision_reader: Callable[[], tuple[str, str]] | None = None,
        process_started_monotonic: float | None = None,
    ) -> None:
        self.status_provider = status_provider
        self.latest_resolver = latest_resolver
        self.printer_resolver = printer_resolver
        self.node_stale_after_seconds = int(node_stale_after_seconds)
        self.air_quality_stale_after_seconds = int(air_quality_stale_after_seconds)
        self.printer_stale_after_seconds = int(printer_stale_after_seconds)
        self.dependencies = tuple(dependencies)
        self.collector_timeout_seconds = float(collector_timeout_seconds)
        self.total_budget_seconds = float(total_budget_seconds)
        self.clock = clock
        self.monotonic = monotonic
        self.revision_reader = revision_reader or read_source_revision
        self.process_started_monotonic = (
            _PROCESS_STARTED_MONOTONIC
            if process_started_monotonic is None
            else float(process_started_monotonic)
        )
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None
        self._in_flight: dict[str, Any] = {}

    # -- public ----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        started = self.monotonic()
        deadline = started + self.total_budget_seconds
        generated_at = self.clock()

        collectors: dict[str, Callable[[], _Signal]] = {
            "units": self._collect_units,
            "sensor_freshness": self._collect_sensor_freshness,
            "printer_freshness": self._collect_printer_freshness,
        }
        collected, timed_out = self._run_collectors(collectors, deadline)

        units = collected.get("units")
        unit_states: dict[str, dict[str, Any]] = (
            dict(units.detail.get("units", {})) if units is not None else {}
        )
        signals: dict[str, _Signal] = {
            "sensor_freshness": collected.get("sensor_freshness")
            or _timed_out_signal("sensor freshness"),
            "printer_freshness": collected.get("printer_freshness")
            or _timed_out_signal("printer freshness"),
        }
        # Both derive from the same collected payloads; deriving them here keeps
        # each collector doing exactly one bounded read.
        signals["influx_read"] = _influx_signal(signals["sensor_freshness"])
        signals["home_assistant_source"] = _home_assistant_signal(
            signals["printer_freshness"]
        )

        dependencies = [
            self._dependency(definition, unit_states, signals, units is None)
            for definition in self.dependencies
        ]

        counts = {state: 0 for state in (HEALTHY, DEGRADED, UNAVAILABLE, UNKNOWN)}
        for item in dependencies:
            counts[item["state"]] += 1
        core_states = [item["state"] for item in dependencies if item["core"]]
        overall = (
            max(core_states, key=lambda state: _SEVERITY[state])
            if core_states
            else UNKNOWN
        )

        revision, revision_origin = self._revision()
        return {
            "generated_at_utc": _iso_utc(generated_at),
            "overall_state": overall,
            "states": [HEALTHY, DEGRADED, UNAVAILABLE, UNKNOWN],
            "counts": counts,
            "service": {
                "name": "home-sensor-dashboard",
                "source_revision": revision,
                "source_revision_origin": revision_origin,
                "process_uptime_seconds": max(
                    0, int(self.monotonic() - self.process_started_monotonic)
                ),
            },
            "thresholds": {
                "node_stale_after_seconds": self.node_stale_after_seconds,
                "air_quality_stale_after_seconds": (
                    self.air_quality_stale_after_seconds
                ),
                "printer_stale_after_seconds": self.printer_stale_after_seconds,
            },
            "probe": {
                "collector_timeout_seconds": self.collector_timeout_seconds,
                "total_budget_seconds": self.total_budget_seconds,
                "elapsed_ms": max(0, round((self.monotonic() - started) * 1000)),
                "timed_out": sorted(timed_out),
            },
            "dependencies": dependencies,
        }

    # -- collection ------------------------------------------------------

    def _run_collectors(
        self,
        collectors: Mapping[str, Callable[[], _Signal]],
        deadline: float,
    ) -> tuple[dict[str, _Signal], list[str]]:
        """Run every collector concurrently under one shared deadline.

        A worker that overruns is abandoned rather than waited on: Python
        cannot cancel a running thread, so the honest guarantee is that the
        *response* is bounded, not that the thread stopped. Abandoned work is
        reported as ``unknown`` with the collector named in ``probe.timed_out``.
        """

        results: dict[str, _Signal] = {}
        timed_out: list[str] = []
        executor = self._executor(len(collectors))
        futures: dict[str, Any] = {}
        with self._lock:
            for name, collector in collectors.items():
                pending = self._in_flight.get(name)
                if pending is not None and not pending.done():
                    # A previous snapshot's collector is still stuck. Wait on the
                    # existing future instead of submitting another one, so a
                    # permanently wedged dependency cannot grow one abandoned
                    # thread per request.
                    futures[name] = pending
                    continue
                future = executor.submit(self._guarded, name, collector)
                self._in_flight[name] = future
                futures[name] = future

        for name, future in futures.items():
            remaining = min(
                self.collector_timeout_seconds, deadline - self.monotonic()
            )
            if remaining <= 0:
                timed_out.append(name)
                continue
            try:
                results[name] = future.result(timeout=remaining)
            except FutureTimeout:
                # Deliberately NOT joined. Python cannot cancel a running
                # thread, so the guarantee this endpoint makes is that the
                # *response* is bounded. The abandoned worker keeps its slot in
                # the fixed-size pool and is reported by name below.
                timed_out.append(name)
            except Exception:  # noqa: BLE001 - a probe must not break the report
                LOGGER.warning("health collector %s failed", name, exc_info=True)
                results[name] = _Signal(
                    UNKNOWN, "check failed", {"error": "collector_failed"}
                )
        return results, timed_out

    def _executor(self, workers: int) -> ThreadPoolExecutor:
        """One fixed-size pool for the provider's lifetime.

        Creating a pool per request and letting its context manager close would
        join every worker on exit, which silently undoes the per-collector
        timeout: the slowest dependency would set the response time no matter
        what budget was configured.
        """

        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(
                    max_workers=max(1, workers),
                    thread_name_prefix="health",
                )
            return self._pool

    @staticmethod
    def _guarded(name: str, collector: Callable[[], _Signal]) -> _Signal:
        try:
            return collector()
        except Exception:  # noqa: BLE001 - never leak a dependency's exception text
            LOGGER.warning("health collector %s raised", name, exc_info=True)
            return _Signal(UNKNOWN, "check failed", {"error": "collector_failed"})

    def _collect_units(self) -> _Signal:
        snapshot = self.status_provider.snapshot()
        units: dict[str, dict[str, Any]] = {}
        for service in snapshot.get("services", ()) or ():
            unit = service.get("unit")
            if isinstance(unit, str):
                units[unit] = {
                    "installed": bool(service.get("installed")),
                    "active": bool(service.get("active")),
                    "active_state": service.get("active_state") or UNKNOWN,
                    "sub_state": service.get("sub_state") or UNKNOWN,
                    "uptime_seconds": service.get("uptime_seconds"),
                }
        return _Signal(HEALTHY, "systemd queried", {"units": units})

    def _collect_sensor_freshness(self) -> _Signal:
        if self.latest_resolver is None:
            return _Signal(UNKNOWN, "no sensor resolver configured")
        payload = self.latest_resolver()
        if not isinstance(payload, Mapping):
            return _Signal(UNKNOWN, "sensor snapshot was not readable")
        now = self.clock()
        groups = (
            ("environment", self.node_stale_after_seconds),
            ("air_quality", self.air_quality_stale_after_seconds),
        )
        total = 0
        fresh = 0
        newest: float | None = None
        for key, threshold in groups:
            for device in payload.get(key, ()) or ():
                if not isinstance(device, Mapping):
                    continue
                total += 1
                age = _age_seconds(device.get("last_seen"), now)
                if age is None:
                    continue
                newest = age if newest is None else min(newest, age)
                if age <= threshold:
                    fresh += 1
        detail = {
            "device_count": total,
            "fresh_device_count": fresh,
            "newest_sample_age_seconds": None if newest is None else int(newest),
            "read_ok": True,
        }
        if total == 0:
            return _Signal(UNKNOWN, "no sensor devices are known", detail)
        if fresh == 0:
            return _Signal(
                UNAVAILABLE, "no sensor device has reported recently", detail
            )
        if fresh < total:
            return _Signal(
                DEGRADED,
                f"{total - fresh} of {total} sensor devices are stale",
                detail,
            )
        return _Signal(HEALTHY, f"all {total} sensor devices are reporting", detail)

    def _collect_printer_freshness(self) -> _Signal:
        if self.printer_resolver is None:
            return _Signal(UNKNOWN, "no printer resolver configured")
        payload = self.printer_resolver()
        if not isinstance(payload, Mapping):
            return _Signal(UNKNOWN, "printer snapshot was not readable")
        status = payload.get("status")
        if payload.get("available") is False and status == "not_configured":
            return _Signal(
                UNKNOWN,
                "printer observer is not configured",
                {"configured": False},
            )
        now = self.clock()
        age = _age_seconds(payload.get("observed_at"), now)
        detail = {
            "configured": True,
            "online": bool(payload.get("online")),
            "observation_age_seconds": None if age is None else int(age),
            "source": _safe_token(payload.get("source")),
            "unavailable_reason": _safe_token(payload.get("unavailable_reason")),
        }
        if age is None:
            return _Signal(UNKNOWN, "printer observation has no timestamp", detail)
        if age > self.printer_stale_after_seconds:
            return _Signal(UNAVAILABLE, "printer telemetry is stale", detail)
        if not payload.get("online"):
            return _Signal(DEGRADED, "printer is reported offline", detail)
        return _Signal(HEALTHY, "printer telemetry is current", detail)

    # -- assembly --------------------------------------------------------

    def _dependency(
        self,
        definition: DependencyDefinition,
        unit_states: Mapping[str, dict[str, Any]],
        signals: Mapping[str, _Signal],
        units_unavailable: bool,
    ) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        states: list[str] = []
        summaries: list[str] = []

        if definition.unit is not None:
            process = _process_signal(
                definition.unit, unit_states, units_unavailable
            )
            checks.append(
                {
                    "name": "process",
                    "state": process.state,
                    "summary": process.summary,
                    "detail": process.detail,
                }
            )
            states.append(process.state)
            summaries.append(process.summary)

        if definition.data_signal is not None:
            data = signals.get(definition.data_signal) or _Signal(
                UNKNOWN, "signal was not collected"
            )
            checks.append(
                {
                    "name": "data",
                    "state": data.state,
                    "summary": data.summary,
                    "detail": data.detail,
                }
            )
            states.append(data.state)
            summaries.append(data.summary)

        has_process = definition.unit is not None
        has_data = definition.data_signal is not None
        basis = (
            "process_and_data"
            if has_process and has_data
            else "process_only"
            if has_process
            else "data_only"
            if has_data
            else "none"
        )
        if not states:
            state = UNKNOWN
            summary = "nothing is observed for this dependency"
        else:
            state = max(states, key=lambda item: _SEVERITY[item])
            summary = "; ".join(summaries)

        return {
            "dependency_id": definition.dependency_id,
            "display_name": definition.display_name,
            "description": definition.description,
            "core": definition.core,
            "state": state,
            "summary": summary,
            "basis": basis,
            "unit": definition.unit,
            "checks": checks,
        }

    def _revision(self) -> tuple[str, str]:
        try:
            return self.revision_reader()
        except Exception:  # noqa: BLE001 - build metadata must never break health
            LOGGER.warning("source revision lookup failed", exc_info=True)
            return ("unknown", "unavailable")


# -- signal helpers ------------------------------------------------------


def _process_signal(
    unit: str,
    unit_states: Mapping[str, dict[str, Any]],
    units_unavailable: bool,
) -> _Signal:
    if units_unavailable:
        return _Signal(UNKNOWN, "systemd state was not collected in time")
    state = unit_states.get(unit)
    if state is None:
        return _Signal(UNKNOWN, "unit was not inspected", {"unit_known": False})
    detail = dict(state)
    if not state.get("installed"):
        return _Signal(UNKNOWN, "unit is not installed", detail)
    if state.get("active"):
        return _Signal(HEALTHY, "unit is active", detail)
    if state.get("active_state") == "failed":
        return _Signal(UNAVAILABLE, "unit has failed", detail)
    return _Signal(UNAVAILABLE, "unit is not active", detail)


def _influx_signal(sensor: _Signal) -> _Signal:
    """Influx health is inferred from whether the sensor read worked.

    The dashboard's every reading goes through InfluxDB, so a successful
    ``latest`` read is direct evidence the store answered. A stale-but-readable
    store is an ingest problem, not an InfluxDB problem, so staleness alone does
    not mark InfluxDB degraded here.
    """

    if sensor.state == UNKNOWN and not sensor.detail.get("read_ok"):
        return _Signal(
            UNAVAILABLE,
            "the time-series read did not complete",
            {"read_ok": False},
        )
    return _Signal(HEALTHY, "the time-series read completed", {"read_ok": True})


def _home_assistant_signal(printer: _Signal) -> _Signal:
    """Home Assistant is observed through the telemetry it feeds.

    Probing Home Assistant directly would mean holding its token in the web
    process. Printer telemetry only arrives via Home Assistant, so its freshness
    is a sufficient and credential-free indicator.
    """

    if printer.detail.get("configured") is False:
        return _Signal(UNKNOWN, "no Home Assistant-sourced telemetry is configured")
    if printer.state == UNAVAILABLE:
        return _Signal(
            UNAVAILABLE,
            "no recent telemetry has arrived from Home Assistant",
            {"observed_via": "printer_telemetry"},
        )
    if printer.state == UNKNOWN:
        return _Signal(UNKNOWN, "telemetry freshness is unknown")
    return _Signal(
        HEALTHY,
        "telemetry is arriving from Home Assistant",
        {"observed_via": "printer_telemetry"},
    )


def _timed_out_signal(label: str) -> _Signal:
    return _Signal(UNKNOWN, f"{label} check did not finish in time", {"read_ok": False})


def _age_seconds(value: Any, now: datetime) -> float | None:
    parsed = _parse_utc(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _safe_token(value: Any) -> str | None:
    """Only short, plain identifiers cross into the response.

    These fields originate upstream. Bounding them keeps an unexpected upstream
    string -- or an accidental path or URL -- out of the health payload.
    """

    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 64:
        return None
    if not all(character.isalnum() or character in "_-." for character in token):
        return None
    return token


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


# -- build metadata ------------------------------------------------------

_PROCESS_STARTED_MONOTONIC = time.monotonic()
_REVISION_PATTERN = "0123456789abcdefABCDEF"


def read_source_revision(
    *,
    release_file: Path = RELEASE_FILE,
    source_root: Path = SERVER_ROOT,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[str, str]:
    """Return ``(revision, origin)`` for the deployed source.

    Production is an rsync target, not a checkout, so the deployment procedure
    writes a ``RELEASE`` stamp. A developer checkout has no stamp but does have
    git, so fall back to it. Neither is required: an unknown revision is
    reported plainly rather than guessed.
    """

    try:
        if release_file.is_file():
            stamped = release_file.read_text(encoding="utf-8").strip().splitlines()
            if stamped:
                candidate = stamped[0].strip()
                if _plausible_revision(candidate):
                    return (candidate, "release_file")
    except OSError:
        LOGGER.warning("release stamp could not be read", exc_info=True)

    checkout = _git_checkout_root(source_root)
    if checkout is None:
        return ("unknown", "unavailable")
    try:
        completed = runner(
            ["git", "-C", str(checkout), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ("unknown", "unavailable")
    if int(getattr(completed, "returncode", 1)) != 0:
        return ("unknown", "unavailable")
    candidate = str(getattr(completed, "stdout", "")).strip()
    if _plausible_revision(candidate):
        return (candidate, "git")
    return ("unknown", "unavailable")


def _git_checkout_root(source_root: Path) -> Path | None:
    """Find the enclosing checkout, if there is one.

    The deployed tree is ``/opt/home-sensor/server`` with no git metadata, so
    this returns None there and the stamp file is the only source. A developer
    checkout keeps ``.git`` one level up from ``server/``, and a linked git
    worktree stores it as a file rather than a directory, so test for existence
    rather than for a directory. The walk is bounded so this can never climb to
    an unrelated repository far above the source tree.
    """

    for candidate in (source_root, *list(source_root.parents)[:2]):
        if (candidate / ".git").exists():
            return candidate
    return None


def _plausible_revision(value: str) -> bool:
    """A revision is a short hex id; anything else is reported as unknown.

    The stamp file is deployment-controlled rather than user-controlled, but
    validating it here means a corrupt or half-written stamp cannot put
    arbitrary text into an API response.
    """

    return 7 <= len(value) <= 40 and all(
        character in _REVISION_PATTERN for character in value
    )
