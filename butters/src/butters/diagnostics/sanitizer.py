"""Bounded secret redaction for all untrusted diagnostic data."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|token|private[_-]?key|wifi|psk)",
    re.IGNORECASE,
)
_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key|cookie)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)((?:https?|mqtts?)://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


@dataclass(frozen=True, slots=True)
class SanitizedText:
    text: str
    redactions: tuple[str, ...]
    truncated: bool


def sanitize_text(value: str, *, max_bytes: int = 8192) -> SanitizedText:
    """Redact likely secrets and bound UTF-8 output without raising on bad text."""

    text = value.replace("\x00", "")
    redactions: list[str] = []
    for index, pattern in enumerate(_PATTERNS, start=1):
        replacement = r"\1[REDACTED]" if index in {1, 2, 3} else "[REDACTED]"
        text, count = pattern.subn(replacement, text)
        if count:
            redactions.append(f"secret_pattern_{index}:{count}")
    encoded = text.encode("utf-8", errors="replace")
    truncated = len(encoded) > max_bytes
    if truncated:
        encoded = encoded[:max_bytes]
        text = encoded.decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
    return SanitizedText(text, tuple(redactions), truncated)


def sanitize_value(value: object, *, max_text_bytes: int = 4096) -> tuple[object, tuple[str, ...]]:
    """Recursively sanitize JSON-like data and omit unsupported implementation objects."""

    redactions: list[str] = []

    def clean(item: object, path: str) -> object:
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            sanitized = sanitize_text(item, max_bytes=max_text_bytes)
            redactions.extend(f"{path}:{entry}" for entry in sanitized.redactions)
            return sanitized.text
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, raw_value in item.items():
                key = str(raw_key)[:128]
                child_path = f"{path}.{key}" if path else key
                if _SECRET_KEY.search(key):
                    result[key] = "[REDACTED]"
                    redactions.append(f"{child_path}:sensitive_key")
                else:
                    result[key] = clean(raw_value, child_path)
            return result
        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray)):
            return [clean(child, f"{path}[{index}]") for index, child in enumerate(item[:256])]
        redactions.append(f"{path}:unsupported_type")
        return "[OMITTED]"

    return clean(value, ""), tuple(redactions)
