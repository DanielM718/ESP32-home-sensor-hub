import json
import stat
from pathlib import Path

import pytest
from butters_nas_agent.config import load_agent_credentials
from butters_nas_agent.provision import provision


def test_provision_creates_distinct_private_matching_halves(tmp_path):
    destination = tmp_path / "identity"
    result = provision(destination)
    assert result["identity"] == "nas-primary"
    assert "token" not in result
    assert "command_key" in result  # This is a path, never key material.

    agent_path = destination / "agent-credentials.json"
    key_path = destination / "command.key"
    server_path = destination / "butters-nas-agent.toml"
    for path in (agent_path, key_path, server_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700

    agent = load_agent_credentials(agent_path)
    server = server_path.read_text(encoding="ascii")
    assert agent["command_key"] == key_path.read_text(encoding="ascii").strip()
    assert result["token_sha256"] in server
    assert agent["token"] not in server
    assert agent["command_key"] not in server
    assert str(key_path) in server


def test_provision_is_create_only(tmp_path):
    destination = tmp_path / "identity"
    provision(destination)
    original = json.loads((destination / "agent-credentials.json").read_text())
    with pytest.raises(FileExistsError):
        provision(destination)
    assert json.loads((destination / "agent-credentials.json").read_text()) == original


def test_truenas_custom_app_mounts_secrets_explicitly_read_only():
    compose = (Path(__file__).parents[1] / "truenas-custom-app.compose.yaml").read_text(
        encoding="utf-8"
    )
    assert ":/run/secrets/butters_agent_credentials:ro" in compose
    assert ":/run/secrets/truenas_read_api_key:ro" in compose
    assert "    secrets:" not in compose
    assert "\nsecrets:" not in compose
