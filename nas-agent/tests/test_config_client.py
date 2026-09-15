import hashlib
import json
from pathlib import Path

import pytest
from butters_nas_agent.client import healthcheck, verify_pin
from butters_nas_agent.config import load_agent_credentials, load_api_key, load_config
from butters_nas_agent.protocol import ProtocolError


def _write(path: Path, value: str, mode=0o600):
    path.write_text(value)
    path.chmod(mode)
    return path


def _toml(**changes):
    values = {
        "agent_id": "nas-primary",
        "shutdown": "false",
        "path": "/nas-agent/v1/session",
    }
    values.update(changes)
    return f'''schema_version = 1
[agent]
url = "wss://butters:8443{values["path"]}"
agent_id = "{values["agent_id"]}"
spki_sha256 = "{"a" * 64}"
heartbeat_seconds = 15
health_file = "/run/agent-health"
[truenas]
url = "wss://truenas.local/api/current"
username = "status_agent"
ca_file = "/run/secrets/ca.pem"
timeout_seconds = 5
shutdown_enabled = {values["shutdown"]}
[jellyfin]
url = "http://192.168.1.240:8096"
health_path = "/health"
'''


def test_config_requires_exact_identity_paths_and_default_disabled(tmp_path):
    path = _write(tmp_path / "agent.toml", _toml())
    config = load_config(path)
    assert config.agent_id == "nas-primary"
    assert config.shutdown_enabled is False
    for changed in (
        _toml(agent_id="desktop"),
        _toml(path="/agent/v1/session"),
        _toml().replace("/api/current", "/api/v2.0/system/info"),
    ):
        _write(path, changed)
        with pytest.raises(ValueError, match="invalid_configuration"):
            load_config(path)


def test_config_rejects_group_or_world_writable_file(tmp_path):
    path = _write(tmp_path / "agent.toml", _toml(), 0o622)
    with pytest.raises(ValueError, match="unsafe_configuration"):
        load_config(path)


def test_credentials_are_exact_and_private(tmp_path):
    path = _write(
        tmp_path / "credentials.json",
        json.dumps({"token": "a" * 64, "command_key": "b" * 64}),
    )
    assert load_agent_credentials(path)["token"] == "a" * 64
    _write(path, path.read_text(), 0o640)
    with pytest.raises(ValueError, match="unsafe_agent_credentials"):
        load_agent_credentials(path)


def test_api_key_is_private_and_not_whitespace_bearing(tmp_path):
    path = _write(tmp_path / "key", "1-secret-material")
    assert load_api_key(path) == "1-secret-material"
    _write(path, "secret with spaces")
    with pytest.raises(ValueError, match="invalid_truenas_api_key"):
        load_api_key(path)


def test_healthcheck_requires_recent_agent_written_marker(tmp_path):
    marker = _write(tmp_path / "heartbeat", "")
    stamp = marker.stat().st_mtime
    assert healthcheck(marker, now=stamp + 89)
    assert not healthcheck(marker, now=stamp + 91)
    assert not healthcheck(tmp_path / "missing", now=stamp)


def test_spki_pin_mismatch_fails_before_machine_auth(monkeypatch):
    public = b"reviewed-test-spki"

    class PublicKey:
        def public_bytes(self, *_args):
            return public

    class Certificate:
        def public_key(self):
            return PublicKey()

    class TlsObject:
        def getpeercert(self, *, binary_form):
            assert binary_form is True
            return b"certificate"

    monkeypatch.setattr(
        "butters_nas_agent.client.x509.load_der_x509_certificate",
        lambda _value: Certificate(),
    )
    expected = hashlib.sha256(public).hexdigest()
    verify_pin(TlsObject(), expected)
    with pytest.raises(ProtocolError, match="server_identity_mismatch"):
        verify_pin(TlsObject(), "0" * 64)
