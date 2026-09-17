import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from butters_nas_agent.backends import JellyfinBackend, TailscaleMetricsBackend
from butters_nas_agent.bandwidth import (
    CounterRateSampler,
    DryRunGovernor,
    NetworkTelemetry,
)


class Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def policy_config(**changes):
    value = {
        "effective_capacity_mbps": 30.0,
        "safe_streaming_budget_mbps": 24.0,
        "reserve_mbps": 6.0,
        "minimum_stream_mbps": 3.0,
        "maximum_stream_mbps": 24.0,
        "stream_stability_seconds": 0.0,
        "minimum_change_mbps": 1.0,
        "policy_cooldown_seconds": 0.0,
        "policy_mode": "dry_run",
    }
    value.update(changes)
    return SimpleNamespace(**value)


def session(
    identifier, *, classification="remote", rate=10.0, paused=False, method="transcode"
):
    return {
        "session_id": identifier,
        "user": "Viewer",
        "playing": True,
        "paused": paused,
        "classification": classification,
        "play_method": method,
        "observed_mbps": rate,
        "bitrate_source": "transcode_reported",
        "position_ticks": 1,
        "client": "Web",
        "device": "Browser",
        "item": "Movie",
    }


def network(rate=18.0, quality="good"):
    return {"remote_tx_mbps": rate, "measurement_quality": quality}


def sessions(*items, available=True):
    return {"available": available, "reason": None, "sessions": list(items)}


def test_counter_first_sample_normal_delta_and_ewma_smoothing():
    clock = Clock()
    sampler = CounterRateSampler(
        alpha=0.5, maximum_interval_seconds=30, monotonic=clock
    )
    assert sampler.sample("ts", 100, 200)["reason"] == "first_sample"
    clock.value = 2
    first = sampler.sample("ts", 1_000_100, 2_000_200)
    assert first["raw_tx_mbps"] == 4.0
    assert first["raw_rx_mbps"] == 8.0
    clock.value = 4
    smoothed = sampler.sample("ts", 3_000_100, 6_000_200)
    assert smoothed["raw_tx_mbps"] == 8.0
    assert smoothed["tx_mbps"] == 6.0


@pytest.mark.parametrize(
    ("tx", "rx", "advance", "reason"),
    [
        (None, 2, 1, "missing_counter"),
        (50, 200, 1, "counter_reset"),
        (200, 300, 31, "stale_interval"),
    ],
)
def test_counter_missing_reset_and_stale(tx, rx, advance, reason):
    clock = Clock()
    sampler = CounterRateSampler(alpha=1, maximum_interval_seconds=30, monotonic=clock)
    sampler.sample("ts", 100, 200)
    clock.value = advance
    assert sampler.sample("ts", tx, rx)["reason"] == reason


def test_counter_negative_elapsed_resets_without_false_zero():
    clock = Clock(10)
    sampler = CounterRateSampler(alpha=1, maximum_interval_seconds=30, monotonic=clock)
    sampler.sample("ts", 100, 200)
    clock.value = 9
    result = sampler.sample("ts", 200, 300)
    assert result["reason"] == "non_monotonic_time"
    assert result["tx_mbps"] is None


def test_network_status_does_not_perturb_the_scheduled_counter_window():
    clock = Clock()
    calls = 0

    class TrueNas:
        @staticmethod
        def interface_rate():
            return {
                "available": True,
                "tx_mbps": 2.0,
                "rx_mbps": 1.0,
                "sample_window_seconds": 1.0,
                "reason": None,
            }

    class Tailscale:
        @staticmethod
        def counters():
            nonlocal calls
            calls += 1
            return {
                "tx_bytes": calls * 1_000_000,
                "rx_bytes": calls * 500_000,
                "paths": {"direct_ipv4": {"tx_bytes": calls, "rx_bytes": calls}},
            }

    config = SimpleNamespace(
        network_monitoring_enabled=True,
        smoothing_alpha=1.0,
        maximum_sample_interval_seconds=30.0,
        sample_interval_seconds=5.0,
    )
    telemetry = NetworkTelemetry(
        config, TrueNas(), Tailscale(), monotonic=clock, wall_clock=clock
    )
    first = telemetry.refresh()
    assert first["remote_reason"] == "first_sample"
    assert telemetry.status() == first
    assert calls == 1
    clock.value = 5
    second = telemetry.refresh()
    assert calls == 2
    assert second["remote_tx_mbps"] == 1.6
    assert second["measurement_quality"] == "good"


class Response:
    status = 200
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.body


def jellyfin_config():
    return SimpleNamespace(
        jellyfin_session_monitoring_enabled=True,
        jellyfin_url="http://192.168.1.240:8096",
        timeout_seconds=5,
        local_networks=("192.168.1.0/24",),
        remote_networks=("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
    )


def raw_session(
    endpoint, method="DirectPlay", *, paused=False, active=True, transcode=None
):
    return {
        "Id": "session-1",
        "UserName": "Daniel",
        "Client": "Jellyfin Web",
        "DeviceName": "Browser",
        "RemoteEndPoint": endpoint,
        "IsActive": active,
        "NowPlayingItem": {"Name": "Movie", "Bitrate": 6_500_000},
        "PlayState": {"IsPaused": paused, "PlayMethod": method, "PositionTicks": 10},
        "TranscodingInfo": transcode,
    }


@pytest.mark.parametrize(
    ("endpoint", "classification"),
    [
        ("192.168.1.20:5000", "local"),
        ("100.100.20.30:5000", "remote"),
        ("[fd7a:115c:a1e0::1234]:5000", "remote"),
        ("203.0.113.10:5000", "unknown"),
        (None, "unknown"),
    ],
)
def test_session_server_endpoint_classification(endpoint, classification):
    backend = JellyfinBackend(
        jellyfin_config(), "secret", opener=lambda *_a, **_k: Response(b"[]")
    )
    assert backend._session(raw_session(endpoint))["classification"] == classification


@pytest.mark.parametrize(
    ("method", "expected", "transcode", "rate_source"),
    [
        ("DirectPlay", "direct_play", None, "source_reported"),
        ("DirectStream", "direct_stream", None, "source_reported"),
        ("Transcode", "transcode", {"Bitrate": 4_000_000}, "transcode_reported"),
    ],
)
def test_session_play_methods_and_bitrate(method, expected, transcode, rate_source):
    backend = JellyfinBackend(
        jellyfin_config(), "secret", opener=lambda *_a, **_k: Response(b"[]")
    )
    result = backend._session(raw_session("100.64.0.2", method, transcode=transcode))
    assert result["play_method"] == expected
    assert result["bitrate_source"] == rate_source


def test_sessions_filters_disconnected_and_nonplaying_and_bounds_secret_fields():
    payload = [
        raw_session("100.64.0.2", active=False),
        {"Id": "idle", "NowPlayingItem": None},
        raw_session("192.168.1.5", paused=True),
    ]
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["token"] = request.headers["X-emby-token"]
        return Response(json.dumps(payload).encode())

    result = JellyfinBackend(jellyfin_config(), "secret", opener=opener).sessions()
    assert seen["url"].endswith("/Sessions?activeWithinSeconds=90")
    assert seen["token"] == "secret"
    assert len(result["sessions"]) == 2
    assert result["sessions"][0]["playing"] is False
    assert result["sessions"][1]["paused"] is True
    assert "RemoteEndPoint" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_tailscale_metrics_sums_paths_without_accepting_other_metrics():
    body = b"""# HELP ignored value
tailscaled_outbound_bytes_total{path=\"direct_ipv4\"} 100
tailscaled_outbound_bytes_total{path=\"derp\"} 40
tailscaled_inbound_bytes_total{path=\"direct_ipv4\"} 25
tailscaled_inbound_bytes_total{path=\"derp\"} 5
unrelated_secret_metric 999
"""
    config = SimpleNamespace(
        tailscale_metrics_url="http://100.100.100.100/metrics", timeout_seconds=5
    )
    result = TailscaleMetricsBackend(
        config, opener=lambda *_a, **_k: Response(body)
    ).counters()
    assert result["tx_bytes"] == 140
    assert result["rx_bytes"] == 30
    assert result["paths"]["derp"]["tx_bytes"] == 40
    assert "unrelated" not in json.dumps(result)


@pytest.mark.parametrize(
    ("count", "target"), [(0, None), (1, 24.0), (2, 12.0), (3, 8.0), (4, 6.0)]
)
def test_dry_run_fair_targets(count, target):
    governor = DryRunGovernor(policy_config())
    state = governor.status(
        network(4 * count), sessions(*(session(str(i), rate=4) for i in range(count)))
    )
    assert state["calculated_per_stream_target_mbps"] == target


def test_policy_reserve_other_traffic_floor_and_ceiling():
    governor = DryRunGovernor(
        policy_config(minimum_stream_mbps=4, maximum_stream_mbps=20)
    )
    one = governor.status(network(5), sessions(session("a", rate=2)))
    assert one["calculated_per_stream_target_mbps"] == 20
    three = governor.status(
        network(29), sessions(*(session(str(i), rate=10) for i in range(3)))
    )
    # Reported Jellyfin exceeds total network here, so other traffic is clamped,
    # and the static 24 Mbps pool remains authoritative.
    assert three["other_remote_observed_mbps"] == 0
    assert three["reconciliation_delta_mbps"] == -1
    assert three["measurement_quality"] == "partial"
    constrained = governor.status(network(28), sessions(session("x", rate=2)))
    assert constrained["calculated_per_stream_target_mbps"] >= 4


def test_non_jellyfin_traffic_shrinks_budget_and_direct_play_is_flagged():
    governor = DryRunGovernor(policy_config())
    state = governor.status(
        network(27),
        sessions(
            session("direct", rate=12, method="direct_play"),
            session("transcode", rate=4),
        ),
    )
    assert state["other_remote_observed_mbps"] == 11
    assert state["calculated_per_stream_target_mbps"] == 6.5
    assert state["direct_play_above_target"] == ["direct"]
    assert state["would_enforce"] is True


def test_paused_local_and_unknown_session_handling():
    governor = DryRunGovernor(policy_config())
    state = governor.status(
        network(3),
        sessions(
            session("paused", paused=True),
            session("local", classification="local"),
            session("unknown", classification="unknown", rate=3),
        ),
    )
    assert state["remote_jellyfin_stream_count"] == 0
    assert state["unknown_stream_count"] == 1
    assert state["calculated_per_stream_target_mbps"] == 21


def test_absent_bit_rate_or_remote_counter_stays_unknown_not_zero():
    governor = DryRunGovernor(policy_config())
    state = governor.status(
        network(None, "unavailable"), sessions(session("a", rate=None))
    )
    assert state["remote_jellyfin_observed_mbps"] is None
    assert state["total_remote_observed_mbps"] is None
    assert state["other_remote_observed_mbps"] is None
    assert state["available_headroom_mbps"] is None


def test_hysteresis_stability_threshold_and_cooldown():
    clock = Clock()
    governor = DryRunGovernor(
        policy_config(
            stream_stability_seconds=10,
            minimum_change_mbps=2,
            policy_cooldown_seconds=20,
        ),
        monotonic=clock,
    )
    assert (
        governor.status(network(0), sessions(session("a", rate=1)))[
            "calculated_per_stream_target_mbps"
        ]
        is None
    )
    clock.value = 10
    assert (
        governor.status(network(0), sessions(session("a", rate=1)))[
            "calculated_per_stream_target_mbps"
        ]
        == 24
    )
    clock.value = 11
    held = governor.status(network(0), sessions(session("a"), session("b")))
    assert held["calculated_per_stream_target_mbps"] == 24
    clock.value = 21
    cooldown = governor.status(network(0), sessions(session("a"), session("b")))
    assert cooldown["reason"] == "policy_change_waiting_for_cooldown"
    clock.value = 31
    assert (
        governor.status(network(0), sessions(session("a"), session("b")))[
            "calculated_per_stream_target_mbps"
        ]
        == 12
    )


def test_enforce_mode_is_impossible():
    with pytest.raises(ValueError, match="enforcement_not_supported"):
        DryRunGovernor(policy_config(policy_mode="enforce"))


def test_off_mode_reports_measurements_but_no_policy_target():
    state = DryRunGovernor(policy_config(policy_mode="off")).status(
        network(12), sessions(session("a", rate=10))
    )
    assert state["remote_jellyfin_observed_mbps"] == 10
    assert state["candidate_per_stream_target_mbps"] is None
    assert state["calculated_per_stream_target_mbps"] is None
    assert state["would_enforce"] is False
    assert state["reason"] == "policy_off"
