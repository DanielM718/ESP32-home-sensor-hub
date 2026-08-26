"""Privilege-boundary regressions for the Butters action broker.

Three defects motivated this module:

* the socket's parent runtime directory was created root:root 0700, so the
  butters service account could not traverse to the socket node at all;
* ``BrokerServer.handle`` had no server-side deadline, so a peer that connected
  and then stalled could hold the serially-handled privileged boundary open;
* the root broker performed privileged work without recording anything at its
  own trust boundary.

The systemd tests read the repository's unit, tmpfiles rule, and installer as
text. Nothing here installs, enables, starts, or queries a unit, and no test
touches a production path. The socket tests use ``socketpair`` fixtures so a
stall is a real blocked ``recv`` rather than a mocked one.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import stat
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pytest
import tomllib
from butters.actions.broker import (
    DEFAULT_PEER_TIMEOUT_SECONDS,
    BrokerOperation,
    BrokerServer,
    FixedBrokerConfig,
    FixedBrokerOperations,
)

BUTTERS = Path(__file__).resolve().parents[1]
SOCKET_UNIT = BUTTERS / "systemd" / "butters-action-broker.socket"
SERVICE_UNIT = BUTTERS / "systemd" / "butters-action-broker.service"
WEB_UNIT = BUTTERS / "systemd" / "butters-web.service"
TMPFILES = BUTTERS / "systemd" / "butters-action-broker.tmpfiles.conf"
INSTALLER = BUTTERS / "scripts" / "install-action-broker"
EXAMPLE_CONFIG = BUTTERS / "config" / "action-broker.example.toml"

SERVICE_ACCOUNT = "butters"
RUNTIME_DIRECTORY = "/run/butters-action-broker"
SOCKET_PATH = f"{RUNTIME_DIRECTORY}/broker.sock"


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


SOCKET_DIRECTIVES = _directives(SOCKET_UNIT)


def _tmpfiles_rules() -> list[list[str]]:
    rules = []
    for raw in TMPFILES.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append(line.split())
    return rules


# --------------------------------------------------------------------------- #
# 1. runtime directory traversal, socket node restriction
# --------------------------------------------------------------------------- #


def test_runtime_directory_rule_grants_the_service_account_traversal_only() -> None:
    """The parent of ListenStream= must be traversable by butters, nothing more.

    Group traversal is the whole fix: without the +x group bit the butters uid
    cannot reach the socket node no matter how the node itself is owned, and
    connect() fails EACCES as broker_unavailable.
    """

    rules = _tmpfiles_rules()
    assert len(rules) == 1, "one directory rule, no other runtime state"
    kind, path, mode, owner, group = rules[0][:5]
    assert kind == "d"
    assert path == RUNTIME_DIRECTORY
    assert path == str(Path(SOCKET_PATH).parent)
    assert owner == "root"
    assert group == SERVICE_ACCOUNT

    bits = int(mode, 8)
    assert bits == 0o710
    # Traversal for the service group; no listing, no world access at all.
    assert bits & stat.S_IXGRP
    assert not bits & (stat.S_IRGRP | stat.S_IWGRP)
    assert not bits & (stat.S_IRWXO)
    # Never world- or group-writable, and never setuid/setgid/sticky.
    assert not bits & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)


def test_socket_node_stays_restricted_to_the_single_intended_peer() -> None:
    assert SOCKET_DIRECTIVES["ListenStream"] == [SOCKET_PATH]
    assert SOCKET_DIRECTIVES["SocketUser"] == [SERVICE_ACCOUNT]
    assert SOCKET_DIRECTIVES["SocketGroup"] == [SERVICE_ACCOUNT]
    node_bits = int(SOCKET_DIRECTIVES["SocketMode"][0], 8)
    assert node_bits == 0o600
    # Only the owning uid may connect; group and world have no access to the node
    # even though the group can traverse the directory above it.
    assert not node_bits & (stat.S_IRWXG | stat.S_IRWXO)


def test_directory_mode_fallback_fails_closed_instead_of_widening() -> None:
    """If the tmpfiles rule has not been applied systemd creates the parent itself.

    That parent is root:root, so the mode systemd would use must not be the
    escape hatch: 0710 leaves the broker unreachable rather than reachable by
    every local account.
    """

    fallback = int(SOCKET_DIRECTIVES["DirectoryMode"][0], 8)
    assert fallback == 0o710
    assert not fallback & stat.S_IRWXO
    assert not fallback & stat.S_IWGRP


def test_socket_unit_is_ordered_after_the_rule_that_owns_its_directory() -> None:
    assert "systemd-tmpfiles-setup.service" in SOCKET_DIRECTIVES["After"]
    assert "systemd-tmpfiles-setup.service" in SOCKET_DIRECTIVES["Requires"]
    # A .socket unit creates exec directories only when it spawns a process, and
    # the parent must exist before the socket is bound, so RuntimeDirectory= here
    # would silently do nothing.
    assert "RuntimeDirectory" not in SOCKET_DIRECTIVES
    # The ownership is declarative; no shell step chmods or chowns a live path.
    for key in ("ExecStartPre", "ExecStartPost", "ExecStopPost"):
        assert key not in SOCKET_DIRECTIVES, key
    # Cleanup stays predictable: the node goes with the unit, the tmpfiles-owned
    # directory persists with its declared ownership.
    assert SOCKET_DIRECTIVES["RemoveOnStop"] == ["true"]


def test_the_broker_runs_as_root_and_its_only_peer_runs_as_the_service_account(
    tmp_path: Path,
) -> None:
    service = _directives(SERVICE_UNIT)
    assert service["User"] == ["root"]
    web = _directives(WEB_UNIT)
    assert web["User"] == [SERVICE_ACCOUNT]
    # Group traversal only helps if the peer really runs in that group.
    assert web["Group"] == [SERVICE_ACCOUNT]
    assert "AF_UNIX" in web["RestrictAddressFamilies"][0]

    # The declared modes, applied to a real directory tree, produce exactly the
    # intended shape. Verifying the traversal denial itself would require
    # dropping to another uid, which the suite cannot do unprivileged.
    directory = tmp_path / "butters-action-broker"
    directory.mkdir(mode=0o710)
    node = directory / "broker.sock"
    node.write_bytes(b"")
    node.chmod(0o600)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o710
    assert stat.S_IMODE(node.stat().st_mode) == 0o600


def test_installer_ships_the_rule_without_enabling_any_capability() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "/etc/tmpfiles.d/butters-action-broker.conf" in text
    assert (
        "systemd-tmpfiles --create /etc/tmpfiles.d/butters-action-broker.conf" in text
    )
    # Enabling and starting stay behind explicit operator flags that default off.
    assert "enable_socket=0" in text
    assert "start_socket=0" in text
    for line in text.splitlines():
        statement = line.strip()
        if statement.startswith(("systemctl enable", "systemctl start")):
            assert line.startswith("  "), f"unguarded activation: {statement}"


# --------------------------------------------------------------------------- #
# socket fixtures
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def _peers() -> Iterator[tuple[socket.socket, socket.socket]]:
    """A real connected AF_UNIX pair: SO_PEERCRED reports this process."""

    server_side, client_side = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        yield server_side, client_side
    finally:
        server_side.close()
        client_side.close()


def _request(operation: str, request_id: str = "request_identifier_123") -> bytes:
    return (
        json.dumps(
            {"version": 1, "request_id": request_id, "operation": operation}
        ).encode()
        + b"\n"
    )


def _reply(client_side: socket.socket) -> dict[str, object]:
    client_side.settimeout(5)
    chunks = bytearray()
    while b"\n" not in chunks:
        block = client_side.recv(4096)
        if not block:
            break
        chunks.extend(block)
    return json.loads(bytes(chunks))


def _server(
    handlers: dict[BrokerOperation, object] | None = None,
    *,
    uid: int | None = None,
    timeout: float = 0.3,
) -> BrokerServer:
    return BrokerServer(
        handlers or {},  # type: ignore[arg-type]
        expected_uid=os.getuid() if uid is None else uid,
        peer_timeout_seconds=timeout,
    )


# --------------------------------------------------------------------------- #
# 2. SO_PEERCRED precedes parsing
# --------------------------------------------------------------------------- #


def test_peercred_rejects_an_unauthorized_uid_before_reading_the_request() -> None:
    reads = 0
    calls: list[str] = []

    class CountingSocket:
        def settimeout(self, _timeout: float) -> None:
            return None

        def getsockopt(self, level, option, length):  # type: ignore[no-untyped-def]
            return real.getsockopt(level, option, length)

        def recv(self, count: int) -> bytes:
            nonlocal reads
            reads += 1
            return real.recv(count)

        def sendall(self, value: bytes) -> None:
            real.sendall(value)

    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: calls.append("wake") or {"ok": True}},
        uid=os.getuid() + 1,
    )
    with _peers() as (server_side, client_side):
        real = server_side
        # A payload that would otherwise be rejected as oversized, to show the
        # peer gate decides first.
        client_side.sendall(b"{" + b"x" * 40000 + b"}\n")
        server.handle(CountingSocket())  # type: ignore[arg-type]
        response = _reply(client_side)
    assert response["ok"] is False
    assert response["error_code"] == "peer_denied"
    assert reads == 0, "no request byte may be read from an unauthorized peer"
    assert calls == []


# --------------------------------------------------------------------------- #
# 3, 4, 5. server-side deadline
# --------------------------------------------------------------------------- #


def test_a_peer_that_sends_nothing_times_out_server_side() -> None:
    server = _server(timeout=0.3)
    with _peers() as (server_side, client_side):
        started = time.monotonic()
        server.handle(server_side)
        elapsed = time.monotonic() - started
        response = _reply(client_side)
    assert response["ok"] is False
    assert response["error_code"] == "request_timeout"
    assert 0.2 <= elapsed < 3, elapsed


def test_a_partial_request_that_drips_cannot_extend_the_deadline() -> None:
    """A byte at a time must not re-arm the bound; one deadline covers the read."""

    server = _server(timeout=0.3)
    stop = threading.Event()

    with _peers() as (server_side, client_side):
        payload = b'{"version": 1, "request_id": "request_identifier_123"'

        def drip() -> None:
            for index in range(len(payload)):
                if stop.is_set():
                    return
                try:
                    client_side.sendall(payload[index : index + 1])
                except OSError:
                    return
                time.sleep(0.05)

        writer = threading.Thread(target=drip, daemon=True)
        writer.start()
        started = time.monotonic()
        server.handle(server_side)
        elapsed = time.monotonic() - started
        stop.set()
        response = _reply(client_side)
        writer.join(timeout=5)

    assert response["ok"] is False
    assert response["error_code"] == "request_timeout"
    # 0.05s per byte would run for seconds if each recv re-armed the timeout.
    assert elapsed < 2, elapsed


def test_a_stalled_connection_does_not_wedge_the_next_valid_request() -> None:
    calls: list[str] = []
    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: calls.append("wake") or {"ok": True}},
        timeout=0.3,
    )

    with _peers() as (server_side, _client_side):
        server.handle(server_side)
    assert calls == []

    with _peers() as (server_side, client_side):
        client_side.sendall(_request("desktop.wake", "later_request_identifier"))
        server.handle(server_side)
        response = _reply(client_side)
    assert response["ok"] is True
    assert response["request_id"] == "later_request_identifier"
    assert calls == ["wake"]


def test_the_default_peer_timeout_is_bounded_and_conservative() -> None:
    # Long enough for a healthy client that writes its request immediately after
    # connecting, short enough that a stalled peer cannot camp on the boundary.
    assert 1.0 <= DEFAULT_PEER_TIMEOUT_SECONDS <= 10.0
    assert BrokerServer({}, expected_uid=0).peer_timeout_seconds == (
        DEFAULT_PEER_TIMEOUT_SECONDS
    )


# --------------------------------------------------------------------------- #
# 6, 7, 8. fail-closed behaviour over a real socket
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not json at all\n", "malformed_request"),
        (b'{"version": 1}\n', "malformed_request"),
        (
            json.dumps(
                {
                    "version": 2,
                    "request_id": "request_identifier_123",
                    "operation": "desktop.wake",
                }
            ).encode()
            + b"\n",
            "protocol_mismatch",
        ),
        (_request("shell.run"), "unknown_operation"),
        (_request("desktop.wake", "short"), "invalid_request_id"),
        (
            json.dumps(
                {
                    "version": 1,
                    "request_id": "request_identifier_123",
                    "operation": "desktop.wake",
                    "command": "rm -rf /",
                }
            ).encode()
            + b"\n",
            "malformed_request",
        ),
        (b"{" + b"x" * 40000 + b"}\n", "message_too_large"),
        (_request("desktop.wake") + _request("desktop.wake"), "malformed_message"),
    ],
)
def test_rejected_requests_fail_closed_without_running_an_operation(
    payload: bytes, code: str
) -> None:
    calls: list[str] = []
    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: calls.append("wake") or {"ok": True}},
        timeout=1.0,
    )
    with _peers() as (server_side, client_side):
        client_side.sendall(payload)
        server.handle(server_side)
        response = _reply(client_side)
    assert response["ok"] is False
    assert response["error_code"] == code
    assert response["status"] == {}
    assert calls == []


def test_replay_detection_still_retires_a_request_id() -> None:
    calls: list[str] = []
    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: calls.append("wake") or {"ok": True}},
        timeout=1.0,
    )
    for expected_ok in (True, False):
        with _peers() as (server_side, client_side):
            client_side.sendall(_request("desktop.wake", "replayed_identifier_1"))
            server.handle(server_side)
            response = _reply(client_side)
        assert response["ok"] is expected_ok
    assert response["error_code"] == "replayed_request"
    assert calls == ["wake"]


def test_a_disabled_operation_is_refused_before_any_subprocess(tmp_path: Path) -> None:
    invocations: list[list[str]] = []

    def runner(argv, **_kwargs):
        invocations.append(argv)
        raise AssertionError("a disabled operation must never reach a subprocess")

    config = FixedBrokerConfig(
        "fixed-desktop",
        "fixed-user",
        "00:11:22:33:44:55",
        "192.0.2.255",
        tmp_path / "credentials" / "desktop_ed25519",
        enabled_operations=frozenset(),
    )
    handlers = FixedBrokerOperations(config, runner=runner).handlers()
    assert handlers == {}
    server = _server(handlers, timeout=1.0)
    for index, operation in enumerate(
        ("desktop.wake", "desktop.monitors_off", "host.reboot")
    ):
        with _peers() as (server_side, client_side):
            client_side.sendall(_request(operation, f"disabled_request_{index:06d}"))
            server.handle(server_side)
            response = _reply(client_side)
        assert response["ok"] is False
        assert response["error_code"] == "operation_unavailable"
    assert invocations == []


# --------------------------------------------------------------------------- #
# 9. sanitized audit at the privilege boundary
# --------------------------------------------------------------------------- #

SECRET = "ssh-ed25519-AAAASUPERSECRETKEYMATERIAL"


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "butters.actions.broker"
        and record.getMessage().startswith("butters.broker request ")
    ]


def _field(message: str, name: str) -> str:
    match = re.search(rf"\b{name}=(\S+)", message)
    assert match, f"{name} missing from {message!r}"
    return match.group(1)


def test_audit_records_an_accepted_request_with_enumerated_fields_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: {"accepted": True, "detail": SECRET}},
        timeout=1.0,
    )
    with (
        caplog.at_level(logging.INFO, logger="butters.actions.broker"),
        _peers() as (server_side, client_side),
    ):
        client_side.sendall(_request("desktop.wake", "audited_identifier_001"))
        server.handle(server_side)
        _reply(client_side)

    messages = _audit_records(caplog)
    assert len(messages) == 1, messages
    message = messages[0]
    assert _field(message, "outcome") == "accepted"
    assert _field(message, "result") == "ok"
    assert _field(message, "operation") == "desktop.wake"
    assert _field(message, "request_id") == "audited_identifier_001"
    assert int(_field(message, "peer_uid")) == os.getuid()
    assert int(_field(message, "peer_pid")) == os.getpid()
    # The operation's own status never reaches the journal.
    assert SECRET not in message
    assert "accepted=True" not in message


def test_audit_records_a_rejected_peer_with_its_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server(uid=os.getuid() + 1, timeout=1.0)
    with (
        caplog.at_level(logging.INFO, logger="butters.actions.broker"),
        _peers() as (server_side, client_side),
    ):
        client_side.sendall(_request("desktop.wake"))
        server.handle(server_side)
        _reply(client_side)

    messages = _audit_records(caplog)
    assert len(messages) == 1, messages
    message = messages[0]
    assert _field(message, "outcome") == "rejected"
    assert _field(message, "result") == "peer_denied"
    # Nothing was parsed, so there is no operation or request ID to claim.
    assert _field(message, "operation") == "-"
    assert _field(message, "request_id") == "-"
    assert int(_field(message, "peer_uid")) == os.getuid()
    assert int(_field(message, "peer_pid")) == os.getpid()


def test_audit_never_echoes_a_peer_supplied_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server(timeout=1.0)
    payload = (
        json.dumps(
            {
                "version": 1,
                "request_id": "x" * 400,
                "operation": f"shell.run {SECRET}",
                "secret": SECRET,
            }
        ).encode()
        + b"\n"
    )
    with (
        caplog.at_level(logging.INFO, logger="butters.actions.broker"),
        _peers() as (server_side, client_side),
    ):
        client_side.sendall(payload)
        server.handle(server_side)
        _reply(client_side)

    messages = _audit_records(caplog)
    assert len(messages) == 1, messages
    message = messages[0]
    assert _field(message, "outcome") == "rejected"
    assert _field(message, "operation") == "-"
    assert _field(message, "request_id") == "-"
    assert SECRET not in message
    assert "x" * 400 not in message
    # One bounded line, and no traceback text from the privileged boundary.
    for record in caplog.records:
        assert record.exc_info is None
        assert SECRET not in record.getMessage()
        assert len(record.getMessage()) < 400


def test_an_audit_failure_never_turns_a_refusal_into_a_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import butters.actions.broker as broker_module

    def exploding(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("journal unavailable")

    monkeypatch.setattr(broker_module.LOGGER, "log", exploding)

    calls: list[str] = []
    server = _server(
        {BrokerOperation.DESKTOP_WAKE: lambda: calls.append("wake") or {"ok": True}},
        timeout=1.0,
    )
    # A refusal stays a refusal.
    with _peers() as (server_side, client_side):
        client_side.sendall(_request("shell.run", "unlogged_identifier_001"))
        server.handle(server_side)
        refused = _reply(client_side)
    assert refused["ok"] is False
    assert refused["error_code"] == "unknown_operation"
    assert calls == []
    # And a legitimate request still completes rather than failing open or shut
    # on the logging path.
    with _peers() as (server_side, client_side):
        client_side.sendall(_request("desktop.wake", "unlogged_identifier_002"))
        server.handle(server_side)
        accepted = _reply(client_side)
    assert accepted["ok"] is True
    assert calls == ["wake"]


# --------------------------------------------------------------------------- #
# 10. fixed SSH argv
# --------------------------------------------------------------------------- #


def test_fixed_ssh_argv_keeps_every_host_key_and_authentication_control(
    tmp_path: Path,
) -> None:
    invocations: list[list[str]] = []

    class Result:
        returncode = 0

    config = FixedBrokerConfig(
        "fixed-desktop",
        "fixed-user",
        "00:11:22:33:44:55",
        "192.0.2.255",
        tmp_path / "credentials" / "desktop_ed25519",
        enabled_operations=frozenset({BrokerOperation.DESKTOP_LOCK}),
    )
    handlers = FixedBrokerOperations(
        config,
        runner=lambda argv, **_kwargs: invocations.append(argv) or Result(),
    ).handlers()
    handlers[BrokerOperation.DESKTOP_LOCK]()

    argv = invocations[0]
    options = {argv[index + 1] for index, item in enumerate(argv) if item == "-o"}
    assert argv[0] == "/usr/bin/ssh"
    assert {
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "PreferredAuthentications=publickey",
        "ClearAllForwardings=yes",
        "StrictHostKeyChecking=yes",
        # The pinned known_hosts is a root-owned provisioned file; the client must
        # not learn or rewrite host keys into it. OpenSSH already implies this
        # when UserKnownHostsFile is overridden, so this is an explicit
        # restatement that survives a default change.
        "UpdateHostKeys=no",
        f"UserKnownHostsFile={config.desktop_known_hosts}",
    } <= options
    assert "-T" in argv, "no PTY is allocated"
    assert "-i" in argv and str(config.desktop_key) in argv
    # Nothing relaxes host-key verification or opens a forwarding channel.
    assert not any(
        item.startswith(
            (
                "StrictHostKeyChecking=no",
                "StrictHostKeyChecking=accept-new",
                "UpdateHostKeys=yes",
                "UpdateHostKeys=ask",
                "PermitLocalCommand=yes",
                "ForwardAgent=yes",
                "ForwardX11",
                "GlobalKnownHostsFile",
            )
        )
        for item in options
    )
    assert not any(
        item in {"-A", "-X", "-Y", "-R", "-L", "-D", "-t", "-W"} for item in argv
    )
    # One fixed remote command, still the final argument, and no shell.
    assert argv[-2:] == [
        "fixed-user@fixed-desktop",
        "rundll32.exe user32.dll,LockWorkStation",
    ]


# --------------------------------------------------------------------------- #
# 11. everything stays disabled
# --------------------------------------------------------------------------- #


def test_every_enumerated_operation_ships_disabled() -> None:
    with EXAMPLE_CONFIG.open("rb") as source:
        raw = tomllib.load(source)
    gates = raw["operations"]
    assert set(gates) == {item.value for item in BrokerOperation}
    assert not any(gates.values()), "no operation may ship enabled"


def test_enumerated_but_unimplemented_operations_have_no_handler(
    tmp_path: Path,
) -> None:
    """Environment operations are enumerated only; enabling one grants nothing.

    The Windows forced-command dispatcher does not exist yet either, so the
    SSH-backed desktop operations must stay behind their gates as well.
    """

    def runner(argv, **_kwargs):
        raise AssertionError("no operation may reach a subprocess here")

    environment = frozenset(
        {
            BrokerOperation.HEATER_ON,
            BrokerOperation.HEATER_OFF,
            BrokerOperation.DEHUMIDIFIER_ON,
            BrokerOperation.DEHUMIDIFIER_OFF,
            BrokerOperation.VENTILATION_ON,
            BrokerOperation.VENTILATION_OFF,
        }
    )
    config = FixedBrokerConfig(
        "fixed-desktop",
        "fixed-user",
        "00:11:22:33:44:55",
        "192.0.2.255",
        tmp_path / "credentials" / "desktop_ed25519",
        enabled_operations=environment,
    )
    handlers = FixedBrokerOperations(config, runner=runner).handlers()
    assert handlers == {}

    server = _server(handlers, timeout=1.0)
    with _peers() as (server_side, client_side):
        client_side.sendall(_request("environment.heater_on", "environment_id_000001"))
        server.handle(server_side)
        response = _reply(client_side)
    assert response["ok"] is False
    assert response["error_code"] == "operation_unavailable"
