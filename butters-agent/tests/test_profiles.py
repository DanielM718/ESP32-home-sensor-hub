import pytest
from butters_agent.profile import (
    local_data_root,
    profile_name,
    staging_fault_delays,
)


def test_staging_profile_has_distinct_windows_data_root():
    production = local_data_root(r"C:\Users\tester\AppData\Local", {})
    staging = local_data_root(
        r"C:\Users\tester\AppData\Local", {"profile": "staging"}
    )
    assert production != staging
    assert production.name == "ButtersAgent"
    assert staging.name == "ButtersAgentStaging"
    assert profile_name({"profile": "staging"}) == "staging"


def test_fault_delays_cannot_activate_in_production_profile():
    with pytest.raises(ValueError, match="staging_fault_requires_staging_profile"):
        staging_fault_delays({"staging_fault_ack_delay_seconds": 4})
    assert staging_fault_delays(
        {
            "profile": "staging",
            "agent_id": "desktop-staging",
            "url": "wss://staging.lan:18443/agent/v1/session",
            "staging_fault_ack_delay_seconds": 4,
        }
    ) == (4.0, 0.0)
