"""Reading the browser assets without depending on how they are formatted.

The stylesheet used to be one minified file, and several tests matched raw
substrings inside it — including the absence of a space before a brace. That
made the CSS unrefactorable: splitting it into a token layer, a base layer, a
shared component layer and one layer per surface would have failed tests that
were never about layering.

These helpers read the whole stylesheet set and normalise whitespace before
matching, so a test can assert what a rule *declares* without also asserting
how it was typed. Token references are resolved against the token layer, so a
declaration written as ``var(--accent-ink)`` can still be checked for the
value it actually produces.
"""

from __future__ import annotations

import re
from pathlib import Path

STATIC_ROOT = Path(__file__).resolve().parents[1] / "src/butters/web/static"
ASSET_ROOT = STATIC_ROOT / "assets"

STYLESHEET_NAMES = (
    "tokens.css",
    "base.css",
    "components.css",
    "chat.css",
    "admin.css",
    "portal.css",
)

STYLESHEETS = {
    name: (ASSET_ROOT / name).read_text(encoding="utf-8") for name in STYLESHEET_NAMES
}
ALL_CSS = "\n".join(STYLESHEETS.values())

_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_comments(source: str) -> str:
    return _COMMENTS.sub(" ", source)


def rule(selector: str, source: str | None = None) -> str:
    """The declaration body of the first rule whose selector list matches.

    Matching is exact on the selector, not on the surrounding formatting: the
    same assertion passes whether the file is minified or indented.
    """

    body = _strip_comments(source if source is not None else ALL_CSS)
    pattern = re.compile(
        r"(?:^|[};])\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}",
        re.MULTILINE,
    )
    match = pattern.search(body)
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group(1)


def declarations(selector: str, source: str | None = None) -> dict[str, str]:
    """A rule as ``{property: value}``, with whitespace collapsed."""

    parsed: dict[str, str] = {}
    for statement in rule(selector, source).split(";"):
        if ":" not in statement:
            continue
        name, _, value = statement.partition(":")
        parsed[name.strip()] = " ".join(value.split())
    return parsed


def token(name: str) -> str:
    """The value of one custom property declared on :root in the token layer."""

    match = re.search(
        rf"^\s*{re.escape(name)}\s*:\s*([^;]+);",
        _strip_comments(STYLESHEETS["tokens.css"]),
        re.MULTILINE,
    )
    assert match is not None, f"missing design token {name}"
    return " ".join(match.group(1).split())


def resolve(value: str) -> str:
    """Resolve ``var(--x)`` references, one level deep, to their token value."""

    return re.sub(r"var\(\s*(--[a-z0-9-]+)\s*\)", lambda hit: token(hit.group(1)), value)


def _channel(value: str) -> float:
    channel = int(value, 16) / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    digits = colour.strip().lstrip("#")
    assert len(digits) == 6, f"expected a six-digit hex colour, got {colour!r}"
    red, green, blue = (_channel(digits[index : index + 2]) for index in (0, 2, 4))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first: str, second: str) -> float:
    """WCAG 2 contrast ratio between two resolved hex colours."""

    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
