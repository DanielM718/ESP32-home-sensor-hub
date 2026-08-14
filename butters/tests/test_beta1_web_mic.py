"""Microphone control rendering regressions for the browser chat surface.

The reported iPhone defect was a glyph drawn entirely from CSS borders and a
pseudo-element: it carried no microphone stem or base, and its painted box sat
below the centre of the round button because only the capsule took part in
layout. It is now an inline SVG whose geometry is centred inside its viewBox.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

STATIC_ROOT = Path(__file__).resolve().parents[1] / "src/butters/web/static"
INDEX_HTML = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
STYLES_CSS = (STATIC_ROOT / "assets/styles.css").read_text(encoding="utf-8")
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def _mic_button() -> str:
    match = re.search(r'<button id="mic-button".*?</button>', INDEX_HTML, re.DOTALL)
    assert match is not None, "the microphone control is missing"
    return match.group(0)


def _mic_icon() -> ElementTree.Element:
    match = re.search(r"<svg class=\"mic-icon\".*?</svg>", _mic_button(), re.DOTALL)
    assert match is not None, "the microphone glyph is not an inline SVG"
    return ElementTree.fromstring(match.group(0))


def _rule(selector: str) -> str:
    match = re.search(rf"(?<![\w.-]){re.escape(selector)}{{(.*?)}}", STYLES_CSS)
    assert match is not None, f"missing CSS rule for {selector}"
    return match.group(1)


def test_the_glyph_is_a_valid_inline_svg_with_a_fixed_viewbox() -> None:
    icon = _mic_icon()

    assert icon.tag == f"{SVG_NAMESPACE}svg" or icon.tag == "svg"
    assert icon.get("viewBox") == "0 0 24 24"
    assert icon.get("fill") == "none"
    assert icon.get("stroke") == "currentColor"
    assert icon.get("stroke-width") == "2"


def test_the_glyph_has_the_parts_of_a_microphone() -> None:
    icon = _mic_icon()
    shapes = [child.tag.removeprefix(SVG_NAMESPACE) for child in icon]

    # Capsule, cradle arc, stem, and base: a bare capsule is not recognisable.
    assert shapes == ["rect", "path", "path", "path"]


def test_the_glyph_is_centred_in_its_viewbox_and_cannot_clip() -> None:
    icon = _mic_icon()
    stroke = float(icon.get("stroke-width", "0")) / 2
    left, right, top, bottom = 24.0, 0.0, 24.0, 0.0
    for element in icon:
        for x_value, y_value in _points(element):
            left = min(left, x_value - stroke)
            right = max(right, x_value + stroke)
            top = min(top, y_value - stroke)
            bottom = max(bottom, y_value + stroke)

    assert left >= 0 and right <= 24, "the glyph paints outside its viewBox"
    assert top >= 0 and bottom <= 24, "the glyph paints outside its viewBox"
    assert abs((left + right) / 2 - 12) < 0.01
    assert abs((top + bottom) / 2 - 12) < 0.01


def test_the_control_keeps_an_accessible_label_and_a_pressed_state() -> None:
    button = _mic_button()
    icon = _mic_icon()

    assert 'aria-label="Hold to speak"' in button
    assert 'aria-pressed="false"' in button
    assert icon.get("aria-hidden") == "true"
    assert icon.get("focusable") == "false"


def test_the_button_centres_the_icon_and_keeps_a_mobile_touch_target() -> None:
    rule = _rule(".mic-button")

    assert "align-items:center" in rule and "justify-content:center" in rule
    assert "padding:0" in rule
    # An inline SVG otherwise sits on the text baseline of the inherited font.
    assert "line-height:0" in rule
    for declaration in (
        "width:46px",
        "height:46px",
        "min-width:46px",
        "min-height:46px",
    ):
        assert declaration in rule
    assert "overflow:hidden" not in rule


def test_the_icon_has_an_explicit_block_box() -> None:
    rule = _rule(".mic-icon")

    assert "display:block" in rule
    assert "width:22px" in rule and "height:22px" in rule


def test_the_listening_state_recolours_the_glyph_without_moving_it() -> None:
    rule = _rule(".mic-button.active")

    assert "background:var(--accent)" in rule
    assert "color:#172015" in rule
    # currentColor carries the state, so no per-shape border rules remain.
    assert ".mic-button span" not in STYLES_CSS


def _points(element: ElementTree.Element) -> list[tuple[float, float]]:
    """Extreme points of the supported primitives, in viewBox units."""

    tag = element.tag.removeprefix(SVG_NAMESPACE)
    if tag == "rect":
        x_value = float(element.get("x", "0"))
        y_value = float(element.get("y", "0"))
        return [
            (x_value, y_value),
            (
                x_value + float(element.get("width", "0")),
                y_value + float(element.get("height", "0")),
            ),
        ]
    assert tag == "path", f"unsupported shape: {tag}"
    return _path_points(element.get("d", ""))


def _path_points(commands: str) -> list[tuple[float, float]]:
    """Track a small absolute/relative subset: M, v, h, and a semicircular arc."""

    tokens = re.findall(r"[MvhaA]|-?\d*\.?\d+", commands)
    points: list[tuple[float, float]] = []
    x_value = y_value = 0.0
    index = 0
    while index < len(tokens):
        command = tokens[index]
        index += 1
        if command == "M":
            x_value, y_value = float(tokens[index]), float(tokens[index + 1])
            index += 2
        elif command == "v":
            y_value += float(tokens[index])
            index += 1
        elif command == "h":
            x_value += float(tokens[index])
            index += 1
        elif command in "aA":
            radius = float(tokens[index])
            x_value += float(tokens[index + 5])
            y_value += float(tokens[index + 6])
            # A chord of two radii is a semicircle, so it reaches one radius out.
            points.append((x_value, y_value + radius))
            index += 7
        else:  # pragma: no cover - guards the parser against unseen syntax
            raise AssertionError(f"unsupported path command: {command}")
        points.append((x_value, y_value))
    return points
