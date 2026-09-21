"""The Appearance setting: a small typed preference, not a theme editor.

Two things have to stay true. Meadow — the palette that was reviewed and
accepted — must be reproducible exactly, not approximately. And no theme an
administrator can save may make the interface unreadable or let anything but
a colour through.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from butters.appearance import (
    CUSTOM,
    DEFAULT_APPEARANCE,
    PRESETS,
    SURFACE_TONES,
    AppearanceError,
    AppearanceStore,
    catalog,
    contrast_ratio,
    measure,
    resolve,
    stylesheet,
    validate,
)
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import create_app
from butters.web.service import BetaAssistantService

STATIC = Path(__file__).parents[1] / "src/butters/web/static"
TOKENS_CSS = (STATIC / "assets/tokens.css").read_text(encoding="utf-8")
ADMIN_HTML = (STATIC / "admin.html").read_text(encoding="utf-8")
ADMIN_JS = (STATIC / "assets/admin.js").read_text(encoding="utf-8")

ORIGIN = "https://butters.example.ts.net"
ADMIN = "admin@example.com"


# ============================ Meadow is exact ==============================


def _declared_tokens() -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", "", TOKENS_CSS, flags=re.DOTALL)
    return {
        name: value.strip()
        for name, value in re.findall(r"^\s*(--[a-z-]+):\s*([^;]+);", body, re.MULTILINE)
    }


def test_meadow_reproduces_the_accepted_stylesheet_exactly() -> None:
    """Not "close to". The reviewed palette, value for value."""

    declared = _declared_tokens()
    for name, value in resolve(DEFAULT_APPEARANCE).items():
        assert declared[name] == value, (name, declared[name], value)


def test_the_default_is_meadow_and_meadow_is_warm() -> None:
    assert DEFAULT_APPEARANCE.preset == "meadow"
    assert DEFAULT_APPEARANCE.accent == "#BED2BA"
    assert DEFAULT_APPEARANCE.surface_tone == "warm"
    # The warm tone is the accepted surface family, stored literally so a
    # rounding change in some future derivation cannot move it.
    declared = _declared_tokens()
    for name, value in SURFACE_TONES["warm"].items():
        assert declared[name] == value, name


def test_resetting_from_any_custom_theme_returns_exactly_to_meadow() -> None:
    wandered = validate({"preset": CUSTOM, "accent": "#E2A2AE", "surface_tone": "cool"})
    assert resolve(wandered) != resolve(DEFAULT_APPEARANCE)
    back = validate({"preset": "meadow"})
    assert back == DEFAULT_APPEARANCE
    assert resolve(back) == resolve(DEFAULT_APPEARANCE)


def test_a_preset_ignores_an_accent_or_tone_sent_alongside_it() -> None:
    """A preset is a reviewed pair; it cannot be half-overridden."""

    chosen = validate({"preset": "meadow", "accent": "#FF00FF", "surface_tone": "cool"})
    assert chosen == DEFAULT_APPEARANCE


# ============================== validation =================================


@pytest.mark.parametrize(
    "payload,code",
    [
        ("not-an-object", "invalid_payload"),
        (["meadow"], "invalid_payload"),
        ({"preset": "sunset"}, "unknown_preset"),
        ({"preset": 7}, "unknown_preset"),
        ({"preset": CUSTOM, "accent": "BED2BA"}, "invalid_accent"),
        ({"preset": CUSTOM, "accent": "#BED2B"}, "invalid_accent"),
        ({"preset": CUSTOM, "accent": "#GGGGGG"}, "invalid_accent"),
        ({"preset": CUSTOM, "accent": "red"}, "invalid_accent"),
        ({"preset": CUSTOM, "accent": 1234}, "invalid_accent"),
        ({"preset": CUSTOM, "accent": "#BED2BA", "surface_tone": "neon"}, "invalid_surface_tone"),
        ({"preset": CUSTOM, "accent": "#BED2BA", "surface_tone": 3}, "invalid_surface_tone"),
    ],
)
def test_a_malformed_theme_is_refused_with_its_reason(payload: object, code: str) -> None:
    with pytest.raises(AppearanceError) as refused:
        validate(payload)
    assert refused.value.code == code


@pytest.mark.parametrize(
    "extra",
    [
        {"--danger": "#000000"},
        {"tokens": {"--bg": "#000000"}},
        {"css": ":root{--bg:red}"},
        {"stylesheet": "body{display:none}"},
    ],
)
def test_nothing_but_the_three_typed_fields_is_accepted(extra: dict) -> None:
    """No arbitrary token, no CSS, no second styling channel."""

    with pytest.raises(AppearanceError) as refused:
        validate({"preset": CUSTOM, "accent": "#BED2BA", **extra})
    assert refused.value.code == "unknown_field"


@pytest.mark.parametrize("accent", ["#BED2BA", "#8DAF9B", "#AAC4BC", "#D7CBB2",
                                    "#C3B5E0", "#9EC5E8", "#E8B894", "#D9C77E"])
def test_a_workable_accent_is_accepted_and_stays_readable(accent: str) -> None:
    chosen = validate({"preset": CUSTOM, "accent": accent, "surface_tone": "warm"})
    assert chosen.accent == accent.upper()
    assert all(row["passes"] for row in measure(chosen))


@pytest.mark.parametrize(
    "accent,why",
    [
        ("#6A7A5A", "too dark to be accent text on a near-black background"),
        ("#1B2A6B", "navy disappears into the background"),
        ("#0A0A0A", "near-black accent"),
        ("#808080", "mid grey cannot carry a label"),
        ("#E02020", "saturated red fails its own label"),
    ],
)
def test_an_unreadable_accent_cannot_be_saved(accent: str, why: str) -> None:
    with pytest.raises(AppearanceError) as refused:
        validate({"preset": CUSTOM, "accent": accent, "surface_tone": "warm"})
    assert refused.value.code == "insufficient_contrast"
    # The message has to name the pair, so it can be acted on.
    assert ":1" in refused.value.message, why


@pytest.mark.parametrize("tone", sorted(SURFACE_TONES))
def test_every_surface_tone_is_readable_on_its_own(tone: str) -> None:
    chosen = validate({"preset": CUSTOM, "accent": "#BED2BA", "surface_tone": tone})
    failures = [row for row in measure(chosen) if not row["passes"]]
    assert not failures, failures


@pytest.mark.parametrize("tone", sorted(SURFACE_TONES))
def test_a_tone_keeps_the_elevation_hierarchy(tone: str) -> None:
    """Sunken below background below surface below raised, in every tone."""

    table = SURFACE_TONES[tone]
    order = ["--surface-sunken", "--bg", "--surface", "--surface-raised"]
    ladder = [contrast_ratio(table[name], "#FFFFFF") for name in order]
    assert ladder == sorted(ladder, reverse=True), (tone, ladder)


# ======================= the accent stays the accent =======================


def test_changing_the_accent_never_moves_a_status_colour() -> None:
    """A purple accent must not make a healthy system purple."""

    status = {"--success", "--warning", "--danger", "--info", "--neutral"}
    for accent in ("#BED2BA", "#C3B5E0", "#E8B894", "#9EC5E8"):
        produced = set(resolve(validate({"preset": CUSTOM, "accent": accent})))
        assert not (produced & status), accent
    # And they are still declared by the token layer, untouched.
    declared = _declared_tokens()
    assert declared["--success"] == "#8DAF9B"
    assert declared["--danger"] == "#D89783"
    assert declared["--warning"] == "#D5B87B"


def test_a_theme_only_ever_emits_colour_declarations() -> None:
    sheet = stylesheet(validate({"preset": CUSTOM, "accent": "#C3B5E0", "surface_tone": "cool"}))
    body = re.search(r":root\{(.*?)\n\}", sheet, re.DOTALL).group(1)
    for line in (item.strip() for item in body.strip().splitlines()):
        assert re.fullmatch(r"--[a-z-]+: (#[0-9A-F]{6}|\d{1,3} \d{1,3} \d{1,3});", line), line
    # One rule, one selector, nothing else.
    assert sheet.count("{") == 1 and sheet.count("}") == 1
    assert "@" not in sheet and "</" not in sheet


def test_a_hostile_accent_string_cannot_reach_the_stylesheet() -> None:
    for attempt in ("#BED2BA;}body{display:none", "#BED2BA</style>", "red;--danger:#000"):
        with pytest.raises(AppearanceError):
            validate({"preset": CUSTOM, "accent": attempt})


# ================================= store ===================================


def test_a_saved_theme_is_returned_unchanged(tmp_path: Path) -> None:
    store = AppearanceStore(tmp_path / "state.sqlite3")
    assert store.load() == DEFAULT_APPEARANCE
    chosen = validate({"preset": CUSTOM, "accent": "#C3B5E0", "surface_tone": "cool"})
    store.save(chosen)
    assert AppearanceStore(tmp_path / "state.sqlite3").load() == chosen


@pytest.mark.parametrize(
    "stored",
    [
        "not json at all",
        '{"preset":"sunset"}',
        '{"preset":"custom","accent":"#0A0A0A"}',
        '{"preset":"custom","accent":"nonsense"}',
        '{"preset":"custom","accent":"#BED2BA","future_field":true}',
        "[]",
        "null",
    ],
)
def test_an_unusable_stored_theme_falls_back_to_meadow(tmp_path: Path, stored: str) -> None:
    """A bad row must never keep an administrator out of the page that fixes it."""

    path = tmp_path / "state.sqlite3"
    AppearanceStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO appearance (id, payload) VALUES (1, ?)", (stored,))
    assert AppearanceStore(path).load() == DEFAULT_APPEARANCE


def test_the_registry_is_open_to_more_reviewed_presets() -> None:
    assert set(PRESETS) == {"meadow"}
    listed = {item["id"] for item in catalog()["presets"]}
    assert listed == {"meadow", CUSTOM}
    # The page renders whatever the registry holds; adding one needs no UI edit.
    assert all({"id", "label", "note", "accent", "surface_tone"} <= set(item)
               for item in catalog()["presets"])


# ============================== the service ================================


def _service(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    base = load_assistant_settings()
    settings = replace(
        base,
        diagnostics=replace(base.diagnostics, enabled=False),
        web=replace(
            base.web,
            state_dir=tmp_path,
            development_mode=True,
            admin_identities=(ADMIN,),
            allowed_origins=(ORIGIN,),
        ).validated(),
        remediation=replace(base.remediation, jobs_dir=tmp_path / "jobs"),
    )
    service = BetaAssistantService(settings, DomainVocabulary((), ()), state_dir=tmp_path)

    class Engine:
        initialization_seconds = 0.0

        def close(self):
            return None

    return create_app(settings, DomainVocabulary((), ()), service, stt_engine_factory=Engine)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN, timeout=20
    )


def _headers(identity: str | None = ADMIN) -> dict[str, str]:
    headers = {"Origin": ORIGIN}
    if identity:
        headers["Tailscale-User-Login"] = identity
    return headers


def _run(app, scenario):
    async def main():
        async with _client(app) as http:
            await scenario(http)
        await app.state.shutdown_workers()

    asyncio.run(main())


def test_the_theme_stylesheet_is_served_to_every_surface(tmp_path, monkeypatch) -> None:
    app = _service(tmp_path, monkeypatch)

    async def scenario(http):
        sheet = await http.get("/assets/theme.css")
        assert sheet.status_code == 200
        assert sheet.headers["content-type"].startswith("text/css")
        # Never cached, or a saved change would not reach a device that had
        # already loaded the page once.
        assert sheet.headers["cache-control"] == "no-store"
        assert "--accent: #BED2BA;" in sheet.text
        # It is linked from all three documents, in <head>, so it blocks paint
        # and the default palette is never shown first.
        for page in ("index.html", "portal.html", "admin.html"):
            document = (STATIC / page).read_text()
            head = document[: document.index("</head>")]
            assert '<link rel="stylesheet" href="/assets/theme.css">' in head, page
            assert head.index("theme.css") > head.index("tokens.css"), page

    _run(app, scenario)


def test_reading_and_writing_the_theme_is_administrator_only(tmp_path, monkeypatch) -> None:
    app = _service(tmp_path, monkeypatch)

    async def main():
        # Separate clients: a session is bound to the identity that created
        # it, so the two callers must not share one cookie jar.
        async with _client(app) as anonymous:
            await anonymous.get("/api/session", headers=_headers(None))
            read = await anonymous.get("/api/admin/appearance", headers=_headers(None))
            assert read.status_code in (401, 403)
            token = (await anonymous.get("/api/session", headers=_headers(None))).json()
            write = await anonymous.post(
                "/api/admin/appearance",
                headers={**_headers(None), "X-Butters-CSRF": token.get("csrf_token", "")},
                json={"preset": "meadow"},
            )
            assert write.status_code in (401, 403)
        async with _client(app) as administrator:
            session = await administrator.get("/api/session", headers=_headers())
            mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
            assert (
                await administrator.get("/api/admin/appearance", headers=_headers())
            ).status_code == 200
            assert (
                await administrator.post(
                    "/api/admin/appearance", headers=mutate, json={"preset": "meadow"}
                )
            ).status_code == 200
        await app.state.shutdown_workers()

    asyncio.run(main())


def test_a_theme_survives_a_reload_and_a_preview_does_not(tmp_path, monkeypatch) -> None:
    app = _service(tmp_path, monkeypatch)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}

        candidate = {"preset": CUSTOM, "accent": "#C3B5E0", "surface_tone": "cool"}
        previewed = await http.post(
            "/api/admin/appearance?preview=1", headers=mutate, json=candidate
        )
        assert previewed.status_code == 200
        assert previewed.json()["tokens"]["--accent"] == "#C3B5E0"
        # Nothing was stored, so the served stylesheet is still Meadow.
        assert "--accent: #BED2BA;" in (await http.get("/assets/theme.css")).text
        assert (await http.get("/api/admin/appearance", headers=_headers())).json()[
            "appearance"
        ]["preset"] == "meadow"

        saved = await http.post("/api/admin/appearance", headers=mutate, json=candidate)
        assert saved.status_code == 200
        assert saved.json()["appearance"] == {
            "preset": "custom", "accent": "#C3B5E0", "surface_tone": "cool"
        }
        # Now every surface gets it, without the browser being involved.
        assert "--accent: #C3B5E0;" in (await http.get("/assets/theme.css")).text

        # And a reset returns exactly to the accepted palette.
        reset = await http.post("/api/admin/appearance", headers=mutate, json={"preset": "meadow"})
        assert reset.json()["tokens"] == resolve(DEFAULT_APPEARANCE)
        assert "--accent: #BED2BA;" in (await http.get("/assets/theme.css")).text

    _run(app, scenario)


def test_a_refused_theme_leaves_the_saved_one_active(tmp_path, monkeypatch) -> None:
    app = _service(tmp_path, monkeypatch)

    async def scenario(http):
        session = await http.get("/api/session", headers=_headers())
        mutate = {**_headers(), "X-Butters-CSRF": session.json()["csrf_token"]}
        await http.post("/api/admin/appearance", headers=mutate,
                        json={"preset": CUSTOM, "accent": "#C3B5E0", "surface_tone": "cool"})

        for bad, code in (
            ({"preset": CUSTOM, "accent": "#0A0A0A"}, "insufficient_contrast"),
            ({"preset": CUSTOM, "accent": "nope"}, "invalid_accent"),
            ({"preset": CUSTOM, "accent": "#BED2BA", "--danger": "#000000"}, "unknown_field"),
        ):
            refused = await http.post("/api/admin/appearance", headers=mutate, json=bad)
            assert refused.status_code == 400, bad
            assert refused.json()["error"] == code
        # Untouched by any of them.
        assert "--accent: #C3B5E0;" in (await http.get("/assets/theme.css")).text

    _run(app, scenario)


def test_a_broken_stored_theme_still_serves_a_usable_page(tmp_path, monkeypatch) -> None:
    app = _service(tmp_path, monkeypatch)

    async def scenario(http):
        await http.get("/api/session", headers=_headers())
        with sqlite3.connect(tmp_path / "state.sqlite3") as connection:
            connection.execute(
                "INSERT OR REPLACE INTO appearance (id, payload) VALUES (1, ?)",
                (json.dumps({"preset": "custom", "accent": "#000000"}),),
            )
        sheet = await http.get("/assets/theme.css")
        assert sheet.status_code == 200
        assert "--accent: #BED2BA;" in sheet.text
        # Admin still loads, so the theme can be fixed from the page itself.
        assert (await http.get("/admin", headers=_headers())).status_code == 200
        assert (await http.get("/api/admin/appearance", headers=_headers())).json()[
            "appearance"
        ] == DEFAULT_APPEARANCE.as_dict()

    _run(app, scenario)


def test_the_theme_endpoint_returns_nothing_but_the_theme(tmp_path, monkeypatch) -> None:
    """Reading a colour must not become a way to read Admin state."""

    app = _service(tmp_path, monkeypatch)

    async def scenario(http):
        await http.get("/api/session", headers=_headers())
        body = (await http.get("/api/admin/appearance", headers=_headers())).json()
        assert set(body) == {"appearance", "tokens", "contrast", "presets",
                             "surface_tones", "default"}
        serialized = json.dumps(body)
        for leak in ("api_key", "sk-", "identity", "session_id", "csrf",
                     "fingerprint", "admin@example.com"):
            assert leak not in serialized, leak

    _run(app, scenario)


# ================================ the page =================================


def test_admin_offers_appearance_and_nothing_resembling_a_css_box() -> None:
    assert 'data-panel="appearance"' in ADMIN_HTML
    assert 'id="panel-appearance"' in ADMIN_HTML
    for control in ("appearance-preset", "appearance-tone", "appearance-accent",
                    "appearance-accent-swatch", "appearance-save",
                    "appearance-revert", "appearance-reset", "appearance-contrast",
                    "appearance-preview", "appearance-status"):
        assert f'id="{control}"' in ADMIN_HTML, control
    # A typed model has no free-text token entry and no stylesheet box.
    assert "<textarea" not in ADMIN_HTML[
        ADMIN_HTML.index('id="panel-appearance"') : ADMIN_HTML.index("</section>",
        ADMIN_HTML.index('id="panel-appearance"'))
    ]
    for forbidden in ("custom-css", "css-editor", "token-name", "raw-css"):
        assert forbidden not in ADMIN_HTML, forbidden


def test_the_preview_shows_enough_to_judge_a_theme() -> None:
    panel = ADMIN_HTML[ADMIN_HTML.index('id="appearance-preview"'):]
    panel = panel[: panel.index("</div>\n\n        <details")]
    for element in ("pill", "axis-cell", "primary-button", "secondary-button",
                    "danger-button", "t-meta", "disabled", "<input"):
        assert element in panel, element


def test_the_page_derives_no_colour_of_its_own() -> None:
    """One authority for what an accent implies, and it is the server."""

    section = ADMIN_JS[ADMIN_JS.index("/* ============================== Appearance"):]
    for forbidden in ("hslToRgb", "rgbToHsl", "lighten(", "darken(", "contrastRatio"):
        assert forbidden not in section, forbidden
    # Only Meadow's own accent appears, as a placeholder in the markup; the
    # script itself names no colour.
    assert not re.search(r"#[0-9a-fA-F]{6}", section)
    assert "/api/admin/appearance?preview=1" in section


def test_unsaved_changes_are_announced_and_reversible() -> None:
    section = ADMIN_JS[ADMIN_JS.index("/* ============================== Appearance"):]
    assert "Unsaved changes" in section
    assert "clearThemeTokens" in section
    # Save is offered only for something the server already accepted.
    assert "disabled = !dirty || !appearanceValid" in section
    # A refused save puts the active theme back on screen.
    failure = section[section.index("async function saveAppearance"):]
    failure = failure[: failure.index("\n}\n")]
    assert "The previous theme is still active." in failure
    assert "clearThemeTokens()" in failure


def test_preview_requests_are_coalesced_and_survive_the_rate_limit() -> None:
    """The administrator rate limit is low and a colour well is chatty.

    Without coalescing, a few seconds of dragging the picker spends the burst
    and the page starts calling a perfectly good theme invalid.
    """

    section = ADMIN_JS[ADMIN_JS.index("/* ============================== Appearance"):]
    assert "appearanceInFlight" in section
    assert "appearanceQueued" in section
    # A rate limit is not a verdict on the theme.
    guard = section[section.index("async function previewAppearance"):]
    guard = guard[: guard.index("\n}\n")]
    assert "/rate limit/i.test(message)" in guard
    assert "appearanceValid = false" in guard
    assert guard.index("rate limit") < guard.index("appearanceValid = false")
    # The colour well resolves on commit, not on every drag frame.
    assert 'appearanceSwatch.addEventListener("change"' in section
    wiring = section[section.index('appearanceSwatch.addEventListener("input"'):]
    wiring = wiring[: wiring.index('appearanceSwatch.addEventListener("change"')]
    assert "scheduleAppearancePreview" not in wiring


def test_no_appearance_control_leaks_onto_chat_or_portal() -> None:
    for page in ("index.html", "portal.html"):
        document = (STATIC / page).read_text()
        assert "appearance" not in document.lower().replace("/assets/theme.css", "")
    for script in ("app.js", "portal.js"):
        source = (STATIC / "assets" / script).read_text()
        assert "appearance" not in source.lower()
