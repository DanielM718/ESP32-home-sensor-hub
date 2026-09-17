"""Bounded network sampling and dry-run Jellyfin bandwidth policy.

This module contains no mutation path.  It consumes fixed, operator-owned
sources and produces small JSON-safe observations for the closed NAS protocol.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import ProtocolError


def _number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _mbps(bytes_per_second: float | None) -> float | None:
    return (
        None if bytes_per_second is None else round(bytes_per_second * 8 / 1_000_000, 3)
    )


@dataclass(slots=True)
class _CounterState:
    at: float
    tx: float
    rx: float
    smoothed_tx: float | None = None
    smoothed_rx: float | None = None


class CounterRateSampler:
    """Turn monotonic byte counters into bounded EWMA rates."""

    def __init__(
        self,
        *,
        alpha: float,
        maximum_interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < alpha <= 1 or maximum_interval_seconds <= 0:
            raise ValueError("invalid_sampler_configuration")
        self.alpha = alpha
        self.maximum_interval_seconds = maximum_interval_seconds
        self.monotonic = monotonic
        self._state: dict[str, _CounterState] = {}
        self._lock = threading.Lock()

    def sample(
        self,
        source: str,
        tx_bytes: object,
        rx_bytes: object,
        *,
        sampled_at: float | None = None,
    ) -> dict[str, object]:
        now = self.monotonic() if sampled_at is None else sampled_at
        tx = _number(tx_bytes)
        rx = _number(rx_bytes)
        if tx is None or rx is None or not math.isfinite(now):
            return self._unavailable("missing_counter")
        with self._lock:
            previous = self._state.get(source)
            if previous is None:
                self._state[source] = _CounterState(now, tx, rx)
                return self._unavailable("first_sample")
            elapsed = now - previous.at
            if elapsed <= 0:
                self._state[source] = _CounterState(now, tx, rx)
                return self._unavailable("non_monotonic_time")
            if tx < previous.tx or rx < previous.rx:
                self._state[source] = _CounterState(now, tx, rx)
                return self._unavailable("counter_reset", elapsed)
            if elapsed > self.maximum_interval_seconds:
                self._state[source] = _CounterState(now, tx, rx)
                return self._unavailable("stale_interval", elapsed)
            raw_tx = (tx - previous.tx) / elapsed
            raw_rx = (rx - previous.rx) / elapsed
            smooth_tx = (
                raw_tx
                if previous.smoothed_tx is None
                else self.alpha * raw_tx + (1 - self.alpha) * previous.smoothed_tx
            )
            smooth_rx = (
                raw_rx
                if previous.smoothed_rx is None
                else self.alpha * raw_rx + (1 - self.alpha) * previous.smoothed_rx
            )
            self._state[source] = _CounterState(now, tx, rx, smooth_tx, smooth_rx)
        return {
            "available": True,
            "reason": None,
            "sample_window_seconds": round(elapsed, 3),
            "tx_mbps": _mbps(smooth_tx),
            "rx_mbps": _mbps(smooth_rx),
            "raw_tx_mbps": _mbps(raw_tx),
            "raw_rx_mbps": _mbps(raw_rx),
        }

    @staticmethod
    def _unavailable(reason: str, elapsed: float | None = None) -> dict[str, object]:
        return {
            "available": False,
            "reason": reason,
            "sample_window_seconds": None if elapsed is None else round(elapsed, 3),
            "tx_mbps": None,
            "rx_mbps": None,
            "raw_tx_mbps": None,
            "raw_rx_mbps": None,
        }


class NetworkTelemetry:
    """Combine supported TrueNAS rates with Tailscale monotonic counters."""

    def __init__(
        self,
        config: Any,
        truenas: Any,
        tailscale: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.truenas = truenas
        self.tailscale = tailscale
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.sampler = CounterRateSampler(
            alpha=config.smoothing_alpha,
            maximum_interval_seconds=config.maximum_sample_interval_seconds,
            monotonic=monotonic,
        )
        self._cache_lock = threading.Lock()
        self._cached: dict[str, object] | None = None

    def status(self) -> dict[str, object]:
        """Return the last scheduled sample without advancing counters."""

        with self._cache_lock:
            cached = self._cached
        return dict(cached) if cached is not None else self.refresh()

    def refresh(self) -> dict[str, object]:
        """Collect one sample; only the background cadence should call this."""

        result = self._collect()
        with self._cache_lock:
            self._cached = result
        return dict(result)

    def _collect(self) -> dict[str, object]:
        if not self.config.network_monitoring_enabled:
            return self._disabled()
        physical: dict[str, object]
        try:
            physical = self.truenas.interface_rate()
        except Exception:  # noqa: BLE001 - source details never cross the boundary.
            physical = {
                "available": False,
                "tx_mbps": None,
                "rx_mbps": None,
                "sample_window_seconds": None,
                "reason": "truenas_reporting_unavailable",
            }
        paths: dict[str, dict[str, float]] = {}
        try:
            counters = self.tailscale.counters()
            sampled = self.sampler.sample(
                "tailscale", counters.get("tx_bytes"), counters.get("rx_bytes")
            )
            paths = (
                counters.get("paths", {})
                if isinstance(counters.get("paths"), dict)
                else {}
            )
        except Exception:  # noqa: BLE001 - transport errors are deliberately collapsed.
            sampled = CounterRateSampler._unavailable("tailscale_metrics_unavailable")
        physical_ok = physical.get("available") is True
        remote_ok = sampled.get("available") is True
        quality = (
            "good"
            if physical_ok and remote_ok
            else "partial"
            if physical_ok or remote_ok
            else "unavailable"
        )
        return {
            "sample_timestamp": self.wall_clock(),
            "nas_total_tx_mbps": physical.get("tx_mbps") if physical_ok else None,
            "nas_total_rx_mbps": physical.get("rx_mbps") if physical_ok else None,
            "remote_tx_mbps": sampled.get("tx_mbps") if remote_ok else None,
            "remote_rx_mbps": sampled.get("rx_mbps") if remote_ok else None,
            "remote_path_counters": paths,
            "physical_source": "truenas_reporting_interface_rate",
            "remote_source": "tailscale_client_metrics_counters",
            "measurement_quality": quality,
            "physical_reason": physical.get("reason"),
            "remote_reason": sampled.get("reason"),
            "sample_window_seconds": sampled.get("sample_window_seconds"),
            "smoothing": f"ewma_alpha_{self.config.smoothing_alpha:g}",
        }

    def _disabled(self) -> dict[str, object]:
        return {
            "sample_timestamp": self.wall_clock(),
            "nas_total_tx_mbps": None,
            "nas_total_rx_mbps": None,
            "remote_tx_mbps": None,
            "remote_rx_mbps": None,
            "remote_path_counters": {},
            "physical_source": "truenas_reporting_interface_rate",
            "remote_source": "tailscale_client_metrics_counters",
            "measurement_quality": "unavailable",
            "physical_reason": "monitoring_disabled",
            "remote_reason": "monitoring_disabled",
            "sample_window_seconds": None,
            "smoothing": f"ewma_alpha_{self.config.smoothing_alpha:g}",
        }


class DryRunGovernor:
    """Stateful, conservative calculator.  It cannot call Jellyfin mutations."""

    def __init__(
        self,
        config: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if config.policy_mode not in {"off", "observe", "dry_run"}:
            raise ValueError("enforcement_not_supported")
        self.config = config
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._observed_count: int | None = None
        self._count_since: float | None = None
        self._accepted_target: float | None = None
        self._changed_at: float | None = None

    def status(
        self, network: dict[str, object], session_state: dict[str, object]
    ) -> dict[str, object]:
        sessions = session_state.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        chargeable = [
            value
            for value in sessions
            if isinstance(value, dict)
            and value.get("playing") is True
            and value.get("paused") is False
            and value.get("classification") in {"remote", "unknown"}
        ]
        remote = [s for s in chargeable if s.get("classification") == "remote"]
        unknown = [s for s in chargeable if s.get("classification") == "unknown"]
        known_rates = [
            float(s["observed_mbps"])
            for s in remote
            if _number(s.get("observed_mbps")) is not None
        ]
        remote_jellyfin = (
            round(sum(known_rates), 3) if len(known_rates) == len(remote) else None
        )
        total_remote = _number(network.get("remote_tx_mbps"))
        other_remote = (
            None
            if total_remote is None or remote_jellyfin is None
            else round(max(0.0, total_remote - remote_jellyfin), 3)
        )
        reconciliation_delta = (
            None
            if total_remote is None or remote_jellyfin is None
            else round(total_remote - remote_jellyfin, 3)
        )
        available_headroom = (
            None
            if total_remote is None
            else round(max(0.0, self.config.effective_capacity_mbps - total_remote), 3)
        )
        dynamic_budget = self.config.safe_streaming_budget_mbps
        if other_remote is not None:
            dynamic_budget = min(
                dynamic_budget,
                max(
                    0.0,
                    self.config.effective_capacity_mbps
                    - self.config.reserve_mbps
                    - other_remote,
                ),
            )
        candidate = None
        infeasible = False
        if chargeable:
            fair = dynamic_budget / len(chargeable)
            infeasible = fair < self.config.minimum_stream_mbps
            candidate = min(
                self.config.maximum_stream_mbps,
                max(self.config.minimum_stream_mbps, fair),
            )
            candidate = round(candidate, 3)
        if self.config.policy_mode == "off":
            candidate = None
            target, held_reason = self._hysteresis(0, None)
        else:
            target, held_reason = self._hysteresis(len(chargeable), candidate)
        above: list[str] = []
        direct_play_above: list[str] = []
        if target is not None:
            for item in chargeable:
                rate = _number(item.get("observed_mbps"))
                if rate is not None and rate > target + self.config.minimum_change_mbps:
                    identifier = str(item.get("session_id", ""))[:64]
                    above.append(identifier)
                    if item.get("play_method") == "direct_play":
                        direct_play_above.append(identifier)
        if self.config.policy_mode == "off":
            reason = "policy_off"
        elif not chargeable:
            reason = "no_active_remote_or_unknown_streams"
        elif infeasible:
            reason = "configured_floor_exceeds_available_fair_share"
        elif other_remote is None:
            reason = "remote_accounting_incomplete_using_static_safe_pool"
        elif held_reason:
            reason = held_reason
        elif direct_play_above:
            reason = "direct_play_above_target_requires_future_renegotiation"
        elif above:
            reason = "one_or_more_sessions_above_dry_run_target"
        else:
            reason = "within_dry_run_target"
        quality = str(network.get("measurement_quality", "unavailable"))
        if session_state.get("available") is not True:
            quality = "unavailable"
        elif (
            unknown
            or remote_jellyfin is None
            or other_remote is None
            or reconciliation_delta is not None
            and reconciliation_delta <= -self.config.minimum_change_mbps
        ):
            quality = "partial" if quality != "unavailable" else quality
        return {
            "effective_capacity_mbps": self.config.effective_capacity_mbps,
            "safe_streaming_budget_mbps": self.config.safe_streaming_budget_mbps,
            "reserve_mbps": self.config.reserve_mbps,
            "remote_jellyfin_stream_count": len(remote),
            "unknown_stream_count": len(unknown),
            "remote_jellyfin_observed_mbps": remote_jellyfin,
            "other_remote_observed_mbps": other_remote,
            "reconciliation_delta_mbps": reconciliation_delta,
            "total_remote_observed_mbps": total_remote,
            "available_headroom_mbps": available_headroom,
            "calculated_per_stream_target_mbps": target,
            "candidate_per_stream_target_mbps": candidate,
            "policy_mode": self.config.policy_mode,
            "measurement_quality": quality,
            "reason": reason,
            "would_enforce": bool(above) and self.config.policy_mode == "dry_run",
            "sessions_above_target": above,
            "direct_play_above_target": direct_play_above,
            "sessions": sessions,
        }

    def _hysteresis(
        self, count: int, candidate: float | None
    ) -> tuple[float | None, str | None]:
        now = self.monotonic()
        with self._lock:
            if count != self._observed_count:
                self._observed_count = count
                self._count_since = now
                if self._accepted_target is not None:
                    return (
                        self._accepted_target,
                        "stream_count_change_waiting_for_stability",
                    )
            if count == 0 or candidate is None:
                self._accepted_target = None
                self._changed_at = now
                return None, None
            if self._accepted_target is None:
                if (
                    self._count_since is not None
                    and now - self._count_since < self.config.stream_stability_seconds
                ):
                    return None, "stream_count_change_waiting_for_stability"
                self._accepted_target = candidate
                self._changed_at = now
                return candidate, None
            if abs(candidate - self._accepted_target) < self.config.minimum_change_mbps:
                return self._accepted_target, "change_below_hysteresis_threshold"
            if (
                self._count_since is not None
                and now - self._count_since < self.config.stream_stability_seconds
            ):
                return (
                    self._accepted_target,
                    "stream_count_change_waiting_for_stability",
                )
            if (
                self._changed_at is not None
                and now - self._changed_at < self.config.policy_cooldown_seconds
            ):
                return self._accepted_target, "policy_change_waiting_for_cooldown"
            self._accepted_target = candidate
            self._changed_at = now
            return candidate, None


class BandwidthService:
    def __init__(
        self, network: NetworkTelemetry, jellyfin: Any, governor: DryRunGovernor
    ) -> None:
        self.network = network
        self.jellyfin = jellyfin
        self.governor = governor
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, object]] | None = None

    def status(self) -> dict[str, object]:
        with self._lock:
            if self._cached is not None:
                age = self.network.monotonic() - self._cached[0]
                if age <= self.network.config.sample_interval_seconds * 2:
                    return dict(self._cached[1])
        return self.refresh()

    def refresh(self) -> dict[str, object]:
        with self._lock:
            result = self._collect()
            self._cached = (self.network.monotonic(), result)
            return dict(result)

    def _collect(self) -> dict[str, object]:
        network = self.network.refresh()
        try:
            sessions = self.jellyfin.sessions()
        except ProtocolError as exc:
            sessions = {"available": False, "reason": str(exc), "sessions": []}
        except Exception:  # noqa: BLE001
            sessions = {
                "available": False,
                "reason": "jellyfin_unavailable",
                "sessions": [],
            }
        return self.governor.status(network, sessions)
