"""Passkey, elevation, and local-console authorization primitives."""

from butters.auth.manager import PasskeyManager, WebAuthnError

__all__ = ["PasskeyManager", "WebAuthnError"]
