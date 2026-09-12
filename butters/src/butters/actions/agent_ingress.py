"""Private TLS transport for the authenticated Desktop Agent WebSocket.

The proxy forwards exactly one machine path to the loopback web daemon. It
strips browser identity by accepting only the WebSocket upgrade allowlist;
AgentHub performs the independent token and signed-frame authentication.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import os
import re
import socket
import ssl
import struct
from dataclasses import dataclass
from pathlib import Path

import tomllib

from butters.actions.file_security import require_private_regular_file

AGENT_PATH = "/agent/v1/session"


@dataclass(frozen=True, slots=True)
class IngressConfig:
    enabled: bool
    bind_interfaces: tuple[str, ...]
    bind_host: str | None
    port: int
    certificate: Path
    private_key: Path
    upstream_host: str
    upstream_port: int


def load_config(path: Path) -> IngressConfig:
    if path.stat().st_mode & 0o022:
        raise ValueError("unsafe_configuration")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    interfaces = data.get("bind_interfaces", [])
    if not isinstance(interfaces, list) or not all(
        isinstance(item, str) for item in interfaces
    ):
        raise ValueError("invalid_bind_interfaces")
    bind_host = data.get("bind_host")
    if bind_host is not None and not isinstance(bind_host, str):
        raise ValueError("invalid_bind_host")
    if bool(interfaces) == bool(bind_host):
        raise ValueError("exactly_one_bind_source_required")
    config = IngressConfig(
        enabled=data.get("enabled") is True,
        bind_interfaces=tuple(interfaces),
        bind_host=bind_host,
        port=int(data.get("port", 8443)),
        certificate=Path(str(data.get("certificate", ""))),
        private_key=Path(str(data.get("private_key", ""))),
        upstream_host=str(data.get("upstream_host", "127.0.0.1")),
        upstream_port=int(data.get("upstream_port", 8090)),
    )
    if not 1024 <= config.port <= 65535 or not 1024 <= config.upstream_port <= 65535:
        raise ValueError("invalid_port")
    if config.upstream_host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("loopback_upstream_required")
    if not config.certificate.is_absolute() or not config.private_key.is_absolute():
        raise ValueError("absolute_tls_paths_required")
    if config.enabled:
        require_private_regular_file(config.private_key, "tls_private_key")
    return config


def bind_addresses(config: IngressConfig | dict[str, object]) -> list[str]:
    """Resolve only private IPv4 addresses from the configured LAN boundary."""

    if isinstance(config, dict):
        interfaces = tuple(config.get("bind_interfaces", ()))
        bind_host = config.get("bind_host")
    else:
        interfaces = config.bind_interfaces
        bind_host = config.bind_host
    if interfaces:
        import fcntl

        addresses: list[str] = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for name in interfaces:
                if not isinstance(name, str) or not re.fullmatch(
                    r"[a-zA-Z0-9_.-]{1,15}", name
                ):
                    raise ValueError("invalid_interface")
                try:
                    packed = struct.pack("256s", name.encode("ascii"))
                    value = fcntl.ioctl(probe.fileno(), 0x8915, packed)
                    addresses.append(socket.inet_ntoa(value[20:24]))
                except OSError:
                    continue
    else:
        if not isinstance(bind_host, str) or not bind_host:
            raise ValueError("bind_source_required")
        addresses = socket.gethostbyname_ex(bind_host)[2]
    if not addresses:
        raise ValueError("private_lan_binding_required")
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if parsed.version != 4 or not parsed.is_private or parsed.is_unspecified:
            raise ValueError("private_lan_binding_required")
    return sorted(set(addresses))


def validate_handshake(raw: bytes) -> bytes:
    """Return a minimal upgrade request or reject browser/auth semantics."""

    if len(raw) > 8192:
        raise ValueError("oversized_handshake")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("malformed_handshake") from exc
    lines = text.split("\r\n")
    if not lines or lines[0] != f"GET {AGENT_PATH} HTTP/1.1":
        raise ValueError("unknown_path")
    allowed = {
        "host",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
    }
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line:
            raise ValueError("malformed_handshake")
        name, value = line.split(":", 1)
        lowered = name.lower()
        if lowered in headers or lowered not in allowed:
            raise ValueError("unexpected_header")
        headers[lowered] = value.strip()
    required = allowed
    if set(headers) != required:
        raise ValueError("missing_upgrade_header")
    try:
        websocket_key = base64.b64decode(headers["sec-websocket-key"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_upgrade") from exc
    connection_tokens = {
        token.strip().lower() for token in headers["connection"].split(",")
    }
    if (
        headers["upgrade"].lower() != "websocket"
        or "upgrade" not in connection_tokens
        or headers["sec-websocket-version"] != "13"
        or len(websocket_key) != 16
    ):
        raise ValueError("invalid_upgrade")
    ordered = (
        "host",
        "upgrade",
        "connection",
        "sec-websocket-key",
        "sec-websocket-version",
    )
    return (
        f"GET {AGENT_PATH} HTTP/1.1\r\n"
        + "\r\n".join(f"{name}: {headers[name]}" for name in ordered)
        + "\r\n\r\n"
    ).encode("ascii")


async def run(config: IngressConfig) -> None:
    if not config.enabled:
        return
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config.certificate, config.private_key)
    active: set[asyncio.StreamWriter] = set()

    async def accept(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream: asyncio.StreamWriter | None = None
        if len(active) >= 4:
            writer.close()
            await writer.wait_closed()
            return
        active.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            clean = validate_handshake(raw)
            source, upstream = await asyncio.wait_for(
                asyncio.open_connection(config.upstream_host, config.upstream_port), 3
            )
            upstream.write(clean)
            await upstream.drain()

            async def copy(
                source_reader: asyncio.StreamReader,
                target_writer: asyncio.StreamWriter,
            ) -> None:
                while data := await asyncio.wait_for(source_reader.read(32768), 60):
                    target_writer.write(data)
                    await target_writer.drain()

            pumps = {
                asyncio.create_task(copy(reader, upstream)),
                asyncio.create_task(copy(source, writer)),
            }
            _done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED
            )
            for pump in pending:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.LimitOverrunError):
            # Do not log request headers: future protocol fields may be sensitive.
            try:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            except (OSError, ConnectionError):
                pass
        finally:
            active.discard(writer)
            writer.close()
            if upstream is not None:
                upstream.close()

    server = await asyncio.start_server(
        accept,
        bind_addresses(config),
        config.port,
        ssl=context,
        ssl_handshake_timeout=5,
        limit=8192,
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    path = Path(
        os.environ.get(
            "BUTTERS_AGENT_INGRESS_CONFIG", "/etc/butters/agent-ingress.toml"
        )
    )
    config = load_config(path)
    if config.enabled:
        asyncio.run(run(config))


if __name__ == "__main__":
    main()
