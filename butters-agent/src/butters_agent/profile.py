"""Fixed Windows data roots for production and isolated staging profiles."""

from pathlib import Path
from urllib.parse import urlsplit


def profile_name(config: dict[str, object]) -> str:
    value = config.get("profile", "production")
    if value not in {"production", "staging"}:
        raise ValueError("invalid_profile")
    return str(value)


def local_data_root(local_app_data: str, config: dict[str, object]) -> Path:
    directory = (
        "ButtersAgentStaging"
        if profile_name(config) == "staging"
        else "ButtersAgent"
    )
    return Path(local_app_data) / directory


def staging_fault_delays(config: dict[str, object]) -> tuple[float, float]:
    """Return bounded delays which are valid only for the staging profile."""

    values = (
        config.get("staging_fault_ack_delay_seconds", 0),
        config.get("staging_fault_result_delay_seconds", 0),
    )
    if any(type(value) not in {int, float} for value in values):
        raise ValueError("invalid_staging_fault_delay")
    delays = tuple(float(value) for value in values)
    if any(value < 0 or value > 35 for value in delays):
        raise ValueError("invalid_staging_fault_delay")
    if any(delays):
        parsed = urlsplit(str(config.get("url", "")))
        if (
            profile_name(config) != "staging"
            or config.get("agent_id") != "desktop-staging"
            or parsed.scheme != "wss"
            or parsed.port != 18443
        ):
            raise ValueError("staging_fault_requires_staging_profile")
    return delays
