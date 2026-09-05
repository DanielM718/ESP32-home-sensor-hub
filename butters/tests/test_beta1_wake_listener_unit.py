"""Deployment regression for the physical "Hey Butters" listener unit.

The listener is the only Butters process that owns physical audio, so its unit
has to relax exactly one thing the web unit locks down (device access) without
relaxing anything else, and it must not be brought up as a side effect of
installing the web assistant.

These tests read the repository's unit file and installer as text. They never
enable, start, or query a running service.
"""

from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

BUTTERS = Path(__file__).resolve().parents[1]
UNIT = BUTTERS / "systemd" / "butters-live.service"
WEB_UNIT = BUTTERS / "systemd" / "butters-web.service"
INSTALLER = BUTTERS / "scripts" / "install-beta1"


def _directives(path: Path) -> dict[str, list[str]]:
    """Collect key -> [values]; systemd allows a key to repeat."""

    values: dict[str, list[str]] = defaultdict(list)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()].append(value.strip())
    return values


UNIT_DIRECTIVES = _directives(UNIT)


def test_runs_the_deployed_runtime_as_the_service_account() -> None:
    assert UNIT_DIRECTIVES["User"] == ["butters"]
    assert UNIT_DIRECTIVES["Group"] == ["butters"]
    assert "root" not in UNIT_DIRECTIVES["User"]
    # The deployed interpreter and tree, never a repository checkout.
    assert UNIT_DIRECTIVES["ExecStart"] == [
        "/opt/butters/.venv/bin/python -m butters live --assistant"
    ]
    assert UNIT_DIRECTIVES["WorkingDirectory"] == ["/opt/butters"]
    assert "PYTHONPATH=/opt/butters/src" in UNIT_DIRECTIVES["Environment"]


def test_uses_the_server_side_audio_and_assistant_configuration() -> None:
    environment = UNIT_DIRECTIVES["Environment"]
    assert "BUTTERS_AUDIO_CONFIG=/opt/butters/config/audio.local.toml" in environment
    assert "BUTTERS_CONFIG=/opt/butters/config/assistant.toml" in environment


def test_a_placeholder_audio_device_is_refused_at_install_time() -> None:
    """The placeholder check belongs to the install, not to this suite.

    config/audio.local.toml is machine-specific and gitignored, so reading it
    here made the suite fail on any fresh clone -- and it checked the developer's
    machine rather than the repository. It also never ran at the only moment it
    matters, because the installer rsyncs that file into the deployed tree where
    butters-live.service points BUTTERS_AUDIO_CONFIG at it.

    The guard now lives beside the installer's development_mode refusal, and
    this pins it as a source contract instead.
    """

    installer = INSTALLER.read_text(encoding="utf-8")

    assert 'if [[ -f "${butters_dir}/config/audio.local.toml" ]]; then' in installer
    assert "grep -q 'CHANGE_ME' \"${butters_dir}/config/audio.local.toml\"" in installer
    # An absent file is a note, not a refusal: the listener is opt-in and this
    # installer never enables it, so the web assistant must still install.
    guard = installer[installer.index("audio.local.toml") :]
    refusal = guard.index("Refusing to install: config/audio.local.toml")
    absent = guard.index("config/audio.local.toml is absent")
    assert refusal < absent
    assert "exit 2" in guard[refusal:absent]


def _audio_guard() -> str:
    """Slice the guard out of the installer so the rehearsal cannot drift.

    The installer refuses to run without root long before this point, so it
    cannot be driven end to end from the suite. Executing the real text is the
    next best thing, and it fails if the block is renamed or removed.
    """

    installer = INSTALLER.read_text(encoding="utf-8")
    start = installer.index("# config/audio.local.toml is machine-specific")
    end = installer.index("if ! getent group butters", start)
    return installer[start:end]


@pytest.mark.parametrize(
    ("contents", "status"),
    [
        ('device = "plughw:CARD=CHANGE_ME,DEV=0"\n', 2),
        ('device = "plughw:CARD=Camera,DEV=0"\n', 0),
        (None, 0),
    ],
    ids=["placeholder-refused", "real-device-accepted", "absent-accepted"],
)
def test_the_audio_guard_refuses_only_the_placeholder(
    tmp_path: Path, contents: str | None, status: int
) -> None:
    """Drive the installer's real guard text against each fixture."""

    (tmp_path / "config").mkdir()
    if contents is not None:
        (tmp_path / "config" / "audio.local.toml").write_text(
            contents, encoding="utf-8"
        )

    script = (
        'set -Eeuo pipefail\n'
        'butters_dir="$1"\n'
        'listener_unit_name="butters-live.service"\n'
    ) + _audio_guard()
    completed = subprocess.run(
        ["bash", "-c", script, "bash", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == status, completed.stderr
    if status == 2:
        assert "CHANGE_ME" in completed.stderr
    elif contents is None:
        assert "audio.local.toml is absent" in completed.stdout


def test_audio_capture_is_permitted_by_an_allow_list_not_by_weak_hardening() -> None:
    """PrivateDevices=true would hide /dev/snd, so the device gate replaces it."""

    assert "PrivateDevices" not in UNIT_DIRECTIVES
    assert UNIT_DIRECTIVES["DevicePolicy"] == ["closed"]
    assert set(UNIT_DIRECTIVES["DeviceAllow"]) == {
        "char-alsa rw",
        "char-video4linux rw",
    }
    # Granting audio to this unit only; the web unit shares the account and must
    # keep its PrivateDevices lock.
    assert UNIT_DIRECTIVES["SupplementaryGroups"] == ["audio video"]
    assert _directives(WEB_UNIT)["PrivateDevices"] == ["true"]


def test_hardening_matches_the_web_unit_where_it_is_not_audio_specific() -> None:
    for directive in (
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectControlGroups",
        "ProtectClock",
        "ProtectHostname",
        "LockPersonality",
        "RestrictSUIDSGID",
    ):
        assert UNIT_DIRECTIVES[directive] == ["true"], directive
    assert UNIT_DIRECTIVES["ProtectSystem"] == ["strict"]
    assert UNIT_DIRECTIVES["ProtectHome"] == ["true"]
    assert UNIT_DIRECTIVES["CapabilityBoundingSet"] == [""]
    assert UNIT_DIRECTIVES["AmbientCapabilities"] == [""]
    # Only the shared action/job state is writable.
    assert UNIT_DIRECTIVES["ReadWritePaths"] == ["/var/lib/butters"]


def test_no_listening_socket_and_no_cloud_credential() -> None:
    """The listener must not widen the web/Tailscale boundary."""

    assert UNIT_DIRECTIVES["RestrictAddressFamilies"] == ["AF_INET AF_INET6 AF_UNIX"]
    environment_files = UNIT_DIRECTIVES["EnvironmentFile"]
    assert environment_files == ["-/etc/butters/butters.conf"]
    # butters.env holds OPENAI_API_KEY; the local listener has no use for it.
    assert not any("butters.env" in item for item in environment_files)


def test_restarts_safely_without_hot_looping() -> None:
    # The capture loop can also end by returning cleanly when the device
    # disappears, which on-failure would not recover.
    assert UNIT_DIRECTIVES["Restart"] == ["always"]
    assert UNIT_DIRECTIVES["RestartSec"] == ["5s"]
    assert int(UNIT_DIRECTIVES["StartLimitBurst"][0]) <= 5
    assert int(UNIT_DIRECTIVES["StartLimitIntervalSec"][0]) >= 60


def test_installer_installs_the_unit_but_never_enables_or_starts_it() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert 'listener_unit_name="butters-live.service"' in text
    installs = [
        line
        for line in lines
        if "${listener_unit_name}" in line and line.strip().startswith("install ")
    ]
    assert len(installs) == 1

    # No *executed* systemctl enable/start/restart may name the listener unit.
    # Echoed guidance is documentation, not an invocation, so it is exempt.
    for line in lines:
        statement = line.strip()
        if statement.startswith("echo "):
            continue
        if "systemctl" in statement and (
            "enable" in statement or "start" in statement or "restart" in statement
        ):
            assert "${listener_unit_name}" not in statement, statement
    assert not any(
        statement.startswith("systemctl") and "butters-live" in statement
        for statement in (line.strip() for line in lines)
    )

    # It is still enableable later.
    assert "[Install]" in UNIT.read_text(encoding="utf-8")
    assert UNIT_DIRECTIVES["WantedBy"] == ["multi-user.target"]

    # The operator is told exactly how, and how to accept the microphone.
    assert "systemctl enable --now ${listener_unit_name}" in text
    assert "journalctl -u ${listener_unit_name} -f" in text
