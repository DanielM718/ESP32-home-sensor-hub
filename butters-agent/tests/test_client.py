from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from butters_agent.client import Client
from butters_agent.engine import Engine
from butters_agent.platform.fake import Platform
from butters_agent.protocol import (
    ProtocolError,
    canonical,
    decode,
    envelope,
    sign,
    verify,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

KEY = b"k" * 32
CONNECTION_ID = "connection"


def client():
    return Client(
        {"agent_id": "desktop"},
        {"command_key": KEY.hex()},
        Engine(Platform()),
    )


def test_rejected_request_error_has_explicit_request_identity():
    request_id = str(uuid.uuid4())
    rejected = sign(
        envelope(
            "request",
            CONNECTION_ID,
            request_id=request_id,
            action="not_allowlisted",
            target="desktop",
            parameters={},
            idempotency_key=str(uuid.uuid4()),
            timeout_seconds=30,
        ),
        KEY,
    )
    unidentifiable = sign(
        envelope(
            "request",
            CONNECTION_ID,
            action="not_allowlisted",
            target="desktop",
            parameters={},
            idempotency_key=str(uuid.uuid4()),
            timeout_seconds=30,
        ),
        KEY,
    )

    class Socket:
        def __init__(self):
            self.incoming = iter(
                (
                    canonical(rejected).decode(),
                    canonical(unidentifiable).decode(),
                )
            )
            self.outgoing = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.incoming)
            except StopIteration:
                raise StopAsyncIteration from None

        async def send(self, raw):
            self.outgoing.append(raw)

    socket = Socket()
    asyncio.run(client()._connected(socket, CONNECTION_ID))
    frames = [verify(decode(raw), KEY, CONNECTION_ID) for raw in socket.outgoing]
    errors = [frame for frame in frames if frame["type"] == "error"]

    assert len(errors) == 2
    assert errors[0]["request_id"] == request_id
    assert "request_id" not in errors[1]


def test_tls_pin_rejection_sends_no_credentials(monkeypatch):
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    sent = []

    class TLS:
        def getpeercert(self, binary_form):
            assert binary_form is True
            return certificate.public_bytes(serialization.Encoding.DER)

    class Transport:
        def get_extra_info(self, name):
            assert name == "ssl_object"
            return TLS()

    class Socket:
        transport = Transport()

        async def send(self, value):
            sent.append(value)

    class Connection:
        async def __aenter__(self):
            return Socket()

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "butters_agent.client.connect",
        lambda *args, **kwargs: Connection(),
    )
    instance = Client(
        {
            "agent_id": "desktop",
            "url": "wss://butters.lan:8443/agent/v1/session",
            "spki_sha256": "0" * 64,
        },
        {"command_key": KEY.hex(), "token": "a" * 64},
        Engine(Platform()),
    )

    with pytest.raises(ProtocolError, match="server_identity_mismatch"):
        asyncio.run(instance.session())
    assert sent == []
