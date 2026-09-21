"""Administrator-chosen appearance, as a small typed model.

This is deliberately *not* a theme editor. An administrator may pick a reviewed
preset, an accent colour, and one of three surface tones. Everything else — the
status palette, the spacing scale, the type scale — stays where the design
system put it, because those are design decisions rather than preferences.

Three properties hold throughout:

1. **Meadow is reproduced exactly, never approximated.** The accepted palette is
   stored here as literal hex, byte for byte as ``tokens.css`` declares it, so a
   reset restores the reviewed state rather than something recomputed from it.
2. **A saved theme can never be unreadable.** Every derived colour is checked
   against the surfaces it will actually sit on before it can be stored, and a
   combination that fails is refused with the specific pair that failed.
3. **Changing the accent changes the accent.** Success, warning, danger and
   informational keep their meanings; a purple accent does not make a healthy
   system purple.

The only thing this module ever emits is a list of ``--token: #rrggbb`` pairs
drawn from its own validated model. No caller-supplied string reaches a
stylesheet.
"""

from __future__ import annotations

import colorsys
import json
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_APPEARANCE",
    "MEADOW",
    "PRESETS",
    "SURFACE_TONES",
    "Appearance",
    "AppearanceError",
    "AppearanceStore",
    "catalog",
    "contrast_ratio",
    "resolve",
    "stylesheet",
    "validate",
]


class AppearanceError(Exception):
    """A rejected appearance, with the reason an administrator can act on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------- colour --

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{6})$")


def _channels(colour: str) -> tuple[float, float, float]:
    digits = colour.lstrip("#")
    return tuple(int(digits[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _hex(red: float, green: float, blue: float) -> str:
    return "#" + "".join(
        f"{round(max(0.0, min(1.0, channel)) * 255):02X}"
        for channel in (red, green, blue)
    )


def _to_hsl(colour: str) -> tuple[float, float, float]:
    red, green, blue = _channels(colour)
    hue, lightness, saturation = colorsys.rgb_to_hls(red, green, blue)
    return hue * 360, saturation * 100, lightness * 100


def _from_hsl(hue: float, saturation: float, lightness: float) -> str:
    return _hex(
        *colorsys.hls_to_rgb(
            (hue % 360) / 360,
            max(0.0, min(100.0, lightness)) / 100,
            max(0.0, min(100.0, saturation)) / 100,
        )
    )


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(colour: str) -> float:
    red, green, blue = (_linear(channel) for channel in _channels(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2 contrast ratio between two ``#rrggbb`` colours."""

    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _rgb_triplet(colour: str) -> str:
    digits = colour.lstrip("#")
    return " ".join(str(int(digits[index : index + 2], 16)) for index in (0, 2, 4))


# --------------------------------------------------------- surface tones --
#
# Three reviewed surface families. Each keeps the lightness and saturation
# structure of the accepted palette and moves only hue, so the elevation
# hierarchy and every ink/surface contrast survive the change. They are stored
# as literals rather than recomputed at import, so "warm" is provably identical
# to what tokens.css ships and cannot drift by a rounding change.

SURFACE_TONE_KEYS = (
    "--bg",
    "--surface",
    "--surface-raised",
    "--surface-sunken",
    "--border",
    "--border-strong",
    "--text",
    "--text-secondary",
    "--text-muted",
    "--text-disabled",
)

SURFACE_TONES: dict[str, dict[str, str]] = {
    # Exactly the accepted palette. Do not regenerate these.
    "warm": {
        "--bg": "#11120F",
        "--surface": "#1A1C17",
        "--surface-raised": "#22261F",
        "--surface-sunken": "#0B0D0A",
        "--border": "#363A31",
        "--border-strong": "#4F5747",
        "--text": "#EAE5DC",
        "--text-secondary": "#B0BFB6",
        "--text-muted": "#86988D",
        "--text-disabled": "#819288",
    },
    "neutral": {
        "--bg": "#101110",
        "--surface": "#1A1A19",
        "--surface-raised": "#222322",
        "--surface-sunken": "#0C0C0B",
        "--border": "#363635",
        "--border-strong": "#4F514D",
        "--text": "#E3E5E1",
        "--text-secondary": "#B7B9B6",
        "--text-muted": "#8F918D",
        "--text-disabled": "#8A8B88",
    },
    "cool": {
        "--bg": "#0F1012",
        "--surface": "#17191C",
        "--surface-raised": "#1F2226",
        "--surface-sunken": "#0A0B0D",
        "--border": "#31353A",
        "--border-strong": "#474E57",
        "--text": "#DCE4EA",
        "--text-secondary": "#B0B9BF",
        "--text-muted": "#869098",
        "--text-disabled": "#818B92",
    },
}

SURFACE_TONE_LABELS = {
    "warm": "Warm",
    "neutral": "Neutral",
    "cool": "Cool",
}

ACCENT_KEYS = ("--accent", "--accent-strong", "--accent-quiet", "--accent-ink")

# The two inks a derived accent may carry. Both come from the accepted palette.
DARK_INK = "#172015"
LIGHT_INK = "#F2F4EF"


# --------------------------------------------------------------- presets --


@dataclass(frozen=True, slots=True)
class Preset:
    """A reviewed, hand-tuned appearance.

    A preset carries its accent family as literal values rather than deriving
    it, because a designed palette is a set of related choices and not the
    output of a formula. New presets are added here; the Admin page reads the
    registry and needs no change.
    """

    identifier: str
    label: str
    note: str
    surface_tone: str
    accent_tokens: Mapping[str, str]

    @property
    def accent(self) -> str:
        return self.accent_tokens["--accent"]


MEADOW = Preset(
    identifier="meadow",
    label="Meadow",
    note="The default. Sage and sand, derived from the reference palette.",
    surface_tone="warm",
    accent_tokens={
        "--accent": "#BED2BA",
        "--accent-strong": "#8DAF9B",
        "--accent-quiet": "#496554",
        "--accent-ink": "#172015",
    },
)

PRESETS: dict[str, Preset] = {MEADOW.identifier: MEADOW}

CUSTOM = "custom"
DEFAULT_PRESET = MEADOW.identifier


# ----------------------------------------------------------------- model --


@dataclass(frozen=True, slots=True)
class Appearance:
    preset: str = DEFAULT_PRESET
    accent: str = MEADOW.accent
    surface_tone: str = MEADOW.surface_tone

    def as_dict(self) -> dict[str, str]:
        return {
            "preset": self.preset,
            "accent": self.accent,
            "surface_tone": self.surface_tone,
        }


DEFAULT_APPEARANCE = Appearance()


def validate(payload: object) -> Appearance:
    """Parse an untrusted payload into the typed model, or refuse it.

    A preset carries its own reviewed accent and tone, so those fields are
    normalised to the preset's values rather than being accepted alongside it.
    Only ``custom`` reads them from the caller.
    """

    if not isinstance(payload, Mapping):
        raise AppearanceError("invalid_payload", "appearance must be an object")
    unknown = set(payload) - {"preset", "accent", "surface_tone"}
    if unknown:
        # A typed model, not a bag of tokens: an unrecognised field is a bug or
        # an attempt to widen the surface, and either way it is refused.
        raise AppearanceError(
            "unknown_field",
            f"appearance does not accept {', '.join(sorted(unknown))}",
        )

    preset = payload.get("preset", DEFAULT_PRESET)
    if not isinstance(preset, str) or (preset not in PRESETS and preset != CUSTOM):
        raise AppearanceError("unknown_preset", "that theme preset does not exist")
    if preset != CUSTOM:
        chosen = PRESETS[preset]
        return Appearance(preset, chosen.accent, chosen.surface_tone)

    accent = payload.get("accent", MEADOW.accent)
    if not isinstance(accent, str) or not _HEX.match(accent.strip()):
        raise AppearanceError(
            "invalid_accent", "the accent must be a six-digit hex colour, like #BED2BA"
        )
    tone = payload.get("surface_tone", MEADOW.surface_tone)
    if not isinstance(tone, str) or tone not in SURFACE_TONES:
        raise AppearanceError(
            "invalid_surface_tone",
            f"the surface tone must be one of {', '.join(sorted(SURFACE_TONES))}",
        )
    candidate = Appearance(CUSTOM, accent.strip().upper(), tone)
    check_contrast(candidate)
    return candidate


# ------------------------------------------------------------ derivation --


def derive_accent(accent: str, tone: Mapping[str, str]) -> dict[str, str]:
    """Build the accent family a custom colour implies.

    Each derived token is solved for the contrast it has to carry rather than
    nudged by a fixed lightness step: the pressed state must still hold its
    label, and the quiet surface exists to carry primary text.
    """

    hue, saturation, lightness = _to_hsl(accent)
    ink = DARK_INK if contrast_ratio(DARK_INK, accent) >= contrast_ratio(LIGHT_INK, accent) else LIGHT_INK

    # As dark as the pressed state can go while the label still reads on it.
    strong = _from_hsl(hue, saturation, lightness - 6)
    for drop in range(18, 5, -1):
        candidate = _from_hsl(hue, saturation, lightness - drop)
        if contrast_ratio(ink, candidate) >= 4.5:
            strong = candidate
            break

    # As colourful as the quiet surface can be while primary text still reads.
    quiet = _from_hsl(hue, min(saturation, 30.0), 34)
    for level in range(45, 11, -1):
        candidate = _from_hsl(hue, min(saturation, 30.0), level)
        if contrast_ratio(tone["--text"], candidate) >= 4.8:
            quiet = candidate
            break

    return {
        "--accent": accent,
        "--accent-strong": strong,
        "--accent-quiet": quiet,
        "--accent-ink": ink,
    }


def resolve(appearance: Appearance) -> dict[str, str]:
    """The exact token overrides an appearance produces."""

    tone = SURFACE_TONES[appearance.surface_tone]
    if appearance.preset != CUSTOM:
        chosen = PRESETS[appearance.preset]
        tone = SURFACE_TONES[chosen.surface_tone]
        accent = dict(chosen.accent_tokens)
    else:
        accent = derive_accent(appearance.accent, tone)
    tokens = {**tone, **accent}
    tokens["--accent-rgb"] = _rgb_triplet(tokens["--accent"])
    return tokens


# ------------------------------------------------------------- contrast --
#
# Every pair a reader actually has to resolve. The surface tones are reviewed,
# so in practice only the accent-dependent rows can fail — but all of them are
# checked, so a future tone cannot be added without meeting the same bar.

@dataclass(frozen=True, slots=True)
class ContrastRule:
    label: str
    foreground: str
    background: str
    minimum: float


CONTRAST_RULES: tuple[ContrastRule, ...] = (
    ContrastRule("primary text on the app background", "--text", "--bg", 4.5),
    ContrastRule("primary text on a card", "--text", "--surface", 4.5),
    ContrastRule("secondary text on a card", "--text-secondary", "--surface", 4.5),
    ContrastRule("metadata on a raised card", "--text-muted", "--surface-raised", 4.5),
    ContrastRule("form field text", "--text", "--surface-sunken", 4.5),
    ContrastRule("button label on the accent", "--accent-ink", "--accent", 4.5),
    ContrastRule("button label when pressed", "--accent-ink", "--accent-strong", 4.5),
    ContrastRule("accent text on the app background", "--accent", "--bg", 4.5),
    ContrastRule("accent text on a card", "--accent", "--surface", 4.5),
    # Non-text: a focus ring only has to be clearly visible, not readable.
    ContrastRule("focus ring on a raised card", "--accent", "--surface-raised", 3.0),
    ContrastRule("text on the quiet accent surface", "--text", "--accent-quiet", 4.5),
)


def measure(appearance: Appearance) -> list[dict[str, object]]:
    """Every checked pair and the ratio it achieves."""

    tokens = resolve(appearance)
    return [
        {
            "label": rule.label,
            "ratio": round(contrast_ratio(tokens[rule.foreground], tokens[rule.background]), 2),
            "minimum": rule.minimum,
            "passes": contrast_ratio(tokens[rule.foreground], tokens[rule.background]) >= rule.minimum,
        }
        for rule in CONTRAST_RULES
    ]


def check_contrast(appearance: Appearance) -> None:
    """Refuse an appearance that would make something unreadable."""

    failures = [row for row in measure(appearance) if not row["passes"]]
    if not failures:
        return
    worst = failures[0]
    raise AppearanceError(
        "insufficient_contrast",
        f"{worst['label']} would be {worst['ratio']}:1, below the {worst['minimum']}:1 "
        f"this interface requires. Try a lighter, more saturated accent.",
    )


# ----------------------------------------------------------- stylesheet --


def stylesheet(appearance: Appearance) -> str:
    """The override layer, built only from validated model values.

    Nothing caller-supplied is interpolated: every value here is either a
    literal from this module or a ``#rrggbb`` string this module produced, so
    the output cannot carry a declaration, a selector, or a comment.
    """

    tokens = resolve(appearance)
    lines = [
        "/* Generated from the saved Appearance settings. */",
        ":root{",
    ]
    for name in (*SURFACE_TONE_KEYS, *ACCENT_KEYS, "--accent-rgb"):
        value = tokens[name]
        if name != "--accent-rgb" and not _HEX.match(value):
            raise AppearanceError("invalid_token", f"{name} did not resolve to a colour")
        lines.append(f"  {name}: {value};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def catalog() -> dict[str, object]:
    """What the Admin page needs to render the controls, from one read."""

    return {
        "presets": [
            {
                "id": preset.identifier,
                "label": preset.label,
                "note": preset.note,
                "accent": preset.accent,
                "surface_tone": preset.surface_tone,
            }
            for preset in PRESETS.values()
        ]
        + [
            {
                "id": CUSTOM,
                "label": "Custom",
                "note": "Your own accent. Related tones are derived from it; "
                "status colours keep their meanings.",
                "accent": MEADOW.accent,
                "surface_tone": MEADOW.surface_tone,
            }
        ],
        "surface_tones": [
            {"id": key, "label": SURFACE_TONE_LABELS[key]} for key in SURFACE_TONES
        ],
        "default": DEFAULT_APPEARANCE.as_dict(),
    }


def state(appearance: Appearance) -> dict[str, object]:
    """The canonical effective appearance, as every caller sees it."""

    return {
        "appearance": appearance.as_dict(),
        "tokens": resolve(appearance),
        "contrast": measure(appearance),
        **catalog(),
    }


# ---------------------------------------------------------------- store --


class AppearanceStore:
    """One row, in the same runtime-settings database the AI settings use.

    A stored value that no longer validates is not repaired into something
    adjacent: the reviewed default is returned instead, so a bad theme can
    never keep an administrator out of the page that would fix it.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS appearance (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )

    def load(self) -> Appearance:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT payload FROM appearance WHERE id=1").fetchone()
        if row is None:
            return DEFAULT_APPEARANCE
        try:
            return validate(json.loads(row[0]))
        except (json.JSONDecodeError, AppearanceError, TypeError):
            # Missing, malformed, or written by a version whose model differed.
            return DEFAULT_APPEARANCE

    def save(self, appearance: Appearance) -> Appearance:
        encoded = json.dumps(appearance.as_dict(), separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO appearance (id, payload) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload, updated_at=CURRENT_TIMESTAMP""",
                (encoded,),
            )
        return appearance

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
