"""TLS transport ingress, not an Action API. Only one WebSocket path is forwarded.

The production web daemon stays loopback-only. Browser identity headers are never
forwarded; agent authentication/HMAC remains end-to-end at AgentHub.
"""

import asyncio
import os
import ssl
import socket
import ipaddress
import re
import struct
import tomllib
from pathlib import Path


def bind_addresses(config):
    """Bind only current private IPv4s on explicitly configured LAN adapters."""
    if "bind_interfaces" not in config:
        addresses = socket.gethostbyname_ex(config["bind_host"])[2]
    else:
        import fcntl
        addresses = []
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for name in config["bind_interfaces"]:
                if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9]{1,15}", name):
                    raise ValueError("invalid_interface")
                try:
                    value = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack("256s", name.encode()))
                    addresses.append(socket.inet_ntoa(value[20:24]))
                except OSError:
                    continue  # An absent/down secondary LAN adapter is optional.
    if not addresses or any(not ipaddress.ip_address(a).is_private or
                            ipaddress.ip_address(a).is_unspecified for a in addresses):
        raise ValueError("private_lan_binding_required")
    return sorted(set(addresses))


def validate_handshake(raw):
    """Strict HTTP upgrade allowlist; never forward identity/cookie/auth headers."""
    if len(raw) > 8192:
        raise ValueError("oversized_handshake")
    lines = raw.decode("ascii").split("\r\n")
    if lines[0] != "GET /agent/v1/session HTTP/1.1":
        raise ValueError("unknown_path")
    headers = {}
    allowed = {"host", "upgrade", "connection", "sec-websocket-key", "sec-websocket-version"}
    for line in lines[1:]:
        if not line:
            continue
        name, value = line.split(":", 1)
        name = name.lower()
        if name in headers or name not in allowed:
            raise ValueError("unexpected_header")
        headers[name] = value.strip()
    if (headers.get("upgrade", "").lower() != "websocket"
            or headers.get("connection", "").lower() != "upgrade"
            or headers.get("sec-websocket-version") != "13"
            or len(headers.get("sec-websocket-key", "")) != 24):
        raise ValueError("invalid_upgrade")
    return ("GET /agent/v1/session HTTP/1.1\r\n" + "\r\n".join(
        key + ": " + value for key, value in headers.items()) + "\r\n\r\n").encode("ascii")


async def run(config):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config["certificate"], config["private_key"])
    active = set()

    async def accept(reader, writer):
        upstream = None
        if len(active) >= 4:
            writer.close()
            return
        active.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            try:
                clean = validate_handshake(raw)
            except ValueError:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            source, upstream = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", 8090), 3)
            upstream.write(clean)
            await upstream.drain()
            async def copy(source, target):
                while data := await asyncio.wait_for(source.read(32768), 60):
                    target.write(data)
                    await target.drain()
            pumps = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(source, writer))]
            try:
                await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for pump in pumps:
                    pump.cancel()
                await asyncio.gather(*pumps, return_exceptions=True)
        except Exception:
            pass  # Never log handshake headers/credentials.
        finally:
            active.discard(writer)
            writer.close()
            if upstream:
                upstream.close()

    server = await asyncio.start_server(accept, bind_addresses(config), config["port"],
        ssl=context, ssl_handshake_timeout=5, limit=8192)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    path = Path(os.environ.get("BUTTERS_AGENT_INGRESS_CONFIG", "/etc/butters/agent-ingress.toml"))
    if path.stat().st_mode & 0o022:
        raise SystemExit("unsafe_configuration")
    asyncio.run(run(tomllib.loads(path.read_text())))
