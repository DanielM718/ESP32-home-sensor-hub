"""SPKI identity binding used before either remote receives a credential."""

from __future__ import annotations

import hashlib
import hmac

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .protocol import ProtocolError


def verify_spki(ssl_object: object, expected: str) -> None:
    certificate = x509.load_der_x509_certificate(
        ssl_object.getpeercert(binary_form=True)  # type: ignore[attr-defined]
    )
    public_key = certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )
    actual = hashlib.sha256(public_key).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise ProtocolError("server_identity_mismatch")
