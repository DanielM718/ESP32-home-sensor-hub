"""Preset validation, promoted-skill degradation, and router precision."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from beta1_harness import admin_headers, build_app, client
from butters.assistant_config import load_assistant_settings
from butters.diagnostics.tools import build_diagnostic_registry
from butters.integrations.project import ProjectInspectionAdapter
from butters.routing.entities import EntityRegistry, MetricRegistry
from butters.routing.router import IntentRouter
from butters.web.speech import SpeechProviderError, VoicePreset, validate_preset


@pytest.mark.parametrize(
    "speed",
    (0.0, -1.0, float("nan"), float("inf"), float("-inf"), 0.1, 9.0),
)
def test_voice_preset_rejects_unsafe_speeds(speed: float) -> None:
    """M-5: an out-of-range or non-finite speed never reaches an engine."""

    preset = VoicePreset("preview", "local", "local-piper", "kathleen", speed, "")
    with pytest.raises(SpeechProviderError) as denied:
        validate_preset(preset)
    assert denied.value.code == "invalid_preset"


def test_voice_preset_rejects_unknown_local_model_and_provider() -> None:
    with pytest.raises(SpeechProviderError):
        validate_preset(VoicePreset("preview", "local", "some-other-model", "kathleen", 1.0, ""))
    with pytest.raises(SpeechProviderError):
        validate_preset(VoicePreset("preview", "elevenlabs", "x", "y", 1.0, ""))


def test_voice_preset_rejects_unconfigured_cloud_model() -> None:
    settings = load_assistant_settings()
    with pytest.raises(SpeechProviderError) as denied:
        validate_preset(
            VoicePreset("preview", "openai", "some-expensive-model", "cedar", 1.0, ""),
            settings=settings,
        )
    assert denied.value.code == "model_denied"


@pytest.mark.parametrize("speed", ("0", "-2", "NaN", "Infinity", "1e9"))
def test_voice_preview_endpoint_validates_before_loading_an_engine(
    tmp_path: Path, speed: str
) -> None:
    """M-5: the HTTP preview path shares the saved-preset validation boundary."""

    async def scenario() -> None:
        app, service, _settings = build_app(tmp_path)

        def exploding_engine(_speed: float):
            raise AssertionError("validation must reject the preset before engine load")

        service.local_tts.engine_factory = exploding_engine
        try:
            async with client(app) as http:
                session = await http.get("/api/session", headers=admin_headers())
                token = session.json()["csrf_token"]
                # A raw body so the non-standard NaN/Infinity literals, which
                # Python's json module accepts, actually reach the server.
                headers = admin_headers("http://testserver", token)
                headers["content-type"] = "application/json"
                response = await http.post(
                    "/api/admin/voice/preview",
                    headers=headers,
                    content=(
                        '{"phrase":"hello","provider":"local","model":"local-piper",'
                        f'"voice":"kathleen","speed":{speed}}}'
                    ).encode(),
                )
                assert response.status_code == 400, speed
                assert response.json()["error"] in {"invalid_preset", "invalid_request"}, speed
        finally:
            await app.state.shutdown_workers()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    (
        "do you support the dashboard",
        "please update the dashboard",
        "is the sensor upstairs reporting",
    ),
)
def test_router_does_not_match_stack_health_on_substring_up(text: str) -> None:
    """L-10: 'support'/'update'/'upstairs' must not imply the health word 'up'."""

    settings = load_assistant_settings()
    router = IntentRouter(EntityRegistry(settings.entities), MetricRegistry())
    routed = router.route(text)
    assert routed.skill != "get_stack_observation", (text, routed.skill)


def test_router_still_matches_genuine_stack_health_questions() -> None:
    settings = load_assistant_settings()
    router = IntentRouter(EntityRegistry(settings.entities), MetricRegistry())
    assert router.route("is the dashboard up").skill == "get_stack_observation"
    assert router.route("is grafana healthy").skill == "get_stack_observation"
    assert router.route("is the mqtt broker running").skill == "get_stack_observation"


def test_interface_observation_degrades_when_enumeration_is_denied(monkeypatch) -> None:
    """M-7: a sandbox denial becomes a typed UNAVAILABLE result, not an exception."""

    from butters.diagnostics import tools

    monkeypatch.setattr(tools, "_list_interface_names", lambda maximum=32: None)
    settings = load_assistant_settings()
    registry = build_diagnostic_registry(settings)
    execution = registry.execute("get_network_interfaces", {})
    assert execution.evidence.status.value in {"unavailable", "degraded"}
    assert execution.evidence.error


def test_interface_enumeration_uses_sysfs_not_netlink() -> None:
    """M-7: the hardened unit withholds AF_NETLINK, so sysfs must be the source."""

    from butters.diagnostics import tools
    import socket as socket_module

    def denied(*_args: object, **_kwargs: object):
        raise OSError("AF_NETLINK is not permitted in this sandbox")

    original = socket_module.if_nameindex
    socket_module.if_nameindex = denied  # type: ignore[assignment]
    try:
        names = tools._list_interface_names()
    finally:
        socket_module.if_nameindex = original  # type: ignore[assignment]
    assert names is not None and "lo" in names


@pytest.mark.parametrize(
    ("skill", "arguments"),
    (
        ("get_host_observation", {"metric": "uptime"}),
        ("get_stack_observation", {"component": "mqtt"}),
        ("get_network_observation", {"view": "interfaces"}),
        ("get_project_status", {"view": "status"}),
    ),
)
def test_promoted_skills_convert_oserror_into_a_typed_failure(
    tmp_path: Path, skill: str, arguments: dict[str, str], monkeypatch
) -> None:
    """M-7: no promoted observation may escape as an unhandled exception."""

    app, service, _settings = build_app(tmp_path)
    try:
        registry = service.assistant.skills

        def explode(*_args: object, **_kwargs: object):
            raise PermissionError("denied by the service sandbox")

        monkeypatch.setattr(
            "butters.diagnostics.tools.DiagnosticToolRegistry.execute", explode
        )
        monkeypatch.setattr(ProjectInspectionAdapter, "inspect", explode)
        execution = registry.execute(skill, arguments, administrator=True)
        assert not execution.ok
        assert execution.failure is not None
        assert execution.failure.code in {"internal_error", "repository_unavailable", "unavailable"}
    finally:
        asyncio.run(app.state.shutdown_workers())


def test_project_inspection_reports_unavailable_without_a_configured_repository() -> None:
    """M-6: an ordinary deployment has no checkout and must say so cleanly."""

    adapter = ProjectInspectionAdapter(None)
    assert adapter.available is False
    with pytest.raises(Exception) as denied:
        adapter.inspect("status")
    assert getattr(denied.value, "code", "") == "repository_unavailable"


def test_project_inspection_reports_unavailable_for_an_unreadable_root(tmp_path: Path) -> None:
    adapter = ProjectInspectionAdapter(tmp_path / "definitely-absent")
    assert adapter.available is False
    with pytest.raises(Exception) as denied:
        adapter.inspect("status")
    assert getattr(denied.value, "code", "") == "repository_unavailable"


def test_project_view_allow_list_is_still_closed(tmp_path: Path) -> None:
    adapter = ProjectInspectionAdapter(tmp_path)
    with pytest.raises(Exception) as denied:
        adapter.inspect("../../etc/passwd")
    assert getattr(denied.value, "code", "") == "policy_denied"
