from butters.actions.nas_agent import NasAgentHub


def session():
    return {
        "session_id": "session-1",
        "user": "Viewer",
        "playing": True,
        "paused": False,
        "classification": "remote",
        "play_method": "transcode",
        "observed_mbps": 10.0,
        "bitrate_source": "transcode_reported",
        "position_ticks": 42,
        "client": "Web",
        "device": "Browser",
        "item": "Movie",
    }


def result(action, payload):
    return {
        "action": action,
        "target": "nas-primary",
        "started_at": 1.0,
        "success": True,
        **payload,
        "completed_at": 2.0,
        "duration_seconds": 1.0,
    }


def test_network_projection_is_exact_and_preserves_unavailable():
    payload = {
        "sample_timestamp": 1.0,
        "nas_total_tx_mbps": 2.0,
        "nas_total_rx_mbps": 3.0,
        "remote_tx_mbps": None,
        "remote_rx_mbps": None,
        "remote_path_counters": {"derp": {"tx_bytes": 10, "rx_bytes": 20}},
        "physical_source": "truenas_reporting_interface_rate",
        "remote_source": "tailscale_client_metrics_counters",
        "measurement_quality": "partial",
        "physical_reason": None,
        "remote_reason": "first_sample",
        "sample_window_seconds": None,
        "smoothing": "ewma_alpha_0.35",
    }
    projected = NasAgentHub._project_result(
        "nas.network.status", result("nas.network.status", payload)
    )
    assert projected["remote_tx_mbps"] is None
    assert (
        NasAgentHub._project_result(
            "nas.network.status",
            result("nas.network.status", {**payload, "interface": "eth0"}),
        )
        is None
    )


def test_session_projection_rejects_endpoint_and_secret_fields():
    payload = {"available": True, "reason": None, "sessions": [session()]}
    assert (
        NasAgentHub._project_result(
            "nas.jellyfin.sessions", result("nas.jellyfin.sessions", payload)
        )["sessions"][0]["classification"]
        == "remote"
    )
    for forbidden in ("RemoteEndPoint", "api_key", "token"):
        tainted = session()
        tainted[forbidden] = "secret"
        assert (
            NasAgentHub._project_result(
                "nas.jellyfin.sessions",
                result("nas.jellyfin.sessions", {**payload, "sessions": [tainted]}),
            )
            is None
        )


def test_bandwidth_projection_allows_only_non_enforcing_modes():
    payload = {
        "effective_capacity_mbps": 30.0,
        "safe_streaming_budget_mbps": 24.0,
        "reserve_mbps": 6.0,
        "remote_jellyfin_stream_count": 1,
        "unknown_stream_count": 0,
        "remote_jellyfin_observed_mbps": 10.0,
        "other_remote_observed_mbps": 1.0,
        "reconciliation_delta_mbps": 1.0,
        "total_remote_observed_mbps": 11.0,
        "available_headroom_mbps": 19.0,
        "calculated_per_stream_target_mbps": 23.0,
        "candidate_per_stream_target_mbps": 23.0,
        "policy_mode": "dry_run",
        "measurement_quality": "good",
        "reason": "within_dry_run_target",
        "would_enforce": False,
        "sessions_above_target": [],
        "direct_play_above_target": [],
        "sessions": [session()],
    }
    assert (
        NasAgentHub._project_result(
            "nas.bandwidth.status", result("nas.bandwidth.status", payload)
        )["policy_mode"]
        == "dry_run"
    )
    assert (
        NasAgentHub._project_result(
            "nas.bandwidth.status",
            result("nas.bandwidth.status", {**payload, "policy_mode": "enforce"}),
        )
        is None
    )


def test_bandwidth_projection_preserves_unknown_session_counts():
    payload = {
        "effective_capacity_mbps": 30.0,
        "safe_streaming_budget_mbps": 24.0,
        "reserve_mbps": 6.0,
        "remote_jellyfin_stream_count": None,
        "unknown_stream_count": None,
        "remote_jellyfin_observed_mbps": None,
        "other_remote_observed_mbps": None,
        "reconciliation_delta_mbps": None,
        "total_remote_observed_mbps": 1.0,
        "available_headroom_mbps": 29.0,
        "calculated_per_stream_target_mbps": None,
        "candidate_per_stream_target_mbps": None,
        "policy_mode": "dry_run",
        "measurement_quality": "unavailable",
        "reason": "jellyfin_session_telemetry_unavailable",
        "would_enforce": False,
        "sessions_above_target": [],
        "direct_play_above_target": [],
        "sessions": [],
    }
    projected = NasAgentHub._project_result(
        "nas.bandwidth.status", result("nas.bandwidth.status", payload)
    )
    assert projected is not None
    assert projected["remote_jellyfin_stream_count"] is None
    assert projected["unknown_stream_count"] is None

    invalid = {**payload, "remote_jellyfin_stream_count": 33}
    assert (
        NasAgentHub._project_result(
            "nas.bandwidth.status", result("nas.bandwidth.status", invalid)
        )
        is None
    )
