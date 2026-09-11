"""Readiness must exercise what it reports.

/readyz used to answer with the string literal "ready" for both
``configuration`` and ``deterministic_router``, so a process whose registries
were empty or whose router raised on every request still returned 200, and the
endpoint was barely distinguishable from the static /healthz beside it.

The helpers are driven directly rather than through HTTP: each one is the unit
that used to be a constant, and driving them is what proves they now look at
something.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from butters.assistant_config import load_assistant_settings
from butters.stt.normalization import DomainVocabulary
from butters.web.app import (
    _broker_check,
    _configuration_check,
    _router_check,
    _state_directory_check,
)
from butters.web.service import BetaAssistantService


@pytest.fixture(scope="module")
def service(tmp_path_factory) -> BetaAssistantService:
    state = tmp_path_factory.mktemp("readiness")
    return BetaAssistantService(
        load_assistant_settings(), DomainVocabulary((), ()), state_dir=state
    )


# --- configuration --------------------------------------------------------


def test_configuration_is_ready_only_when_the_registries_are_populated(
    service: BetaAssistantService,
) -> None:
    assert _configuration_check(service) == "ready"


def test_configuration_is_unavailable_without_entities(
    service: BetaAssistantService,
) -> None:
    """An assistant with no entities cannot answer anything."""

    class Router:
        entities = type("Registry", (), {"entities": ()})()

    class Assistant:
        router = Router()
        skills = service.assistant.skills

    class Stub:
        assistant = Assistant()

    assert _configuration_check(Stub()) == "unavailable"


def test_configuration_never_raises_out_of_the_probe() -> None:
    class Exploding:
        @property
        def assistant(self):
            raise RuntimeError("token abc123 in /etc/butters/butters.env")

    assert _configuration_check(Exploding()) == "unavailable"


# --- router ---------------------------------------------------------------


def test_the_router_check_drives_the_real_router(
    service: BetaAssistantService,
) -> None:
    assert _router_check(service) == "ready"


def test_a_router_that_raises_is_reported_unavailable() -> None:
    class Broken:
        class assistant:  # a stand-in namespace, not a class name
            @staticmethod
            def preview_route(_text: str):
                raise ValueError("normalization exploded")

    assert _router_check(Broken()) == "unavailable"


def test_a_router_returning_nothing_is_reported_unavailable() -> None:
    class Silent:
        class assistant:  # a stand-in namespace, not a class name
            @staticmethod
            def preview_route(_text: str):
                return None

    assert _router_check(Silent()) == "unavailable"


# --- state directory ------------------------------------------------------


def test_state_directory_readiness_requires_writability(tmp_path: Path) -> None:
    """Every ledger is SQLite in here; existence alone is not readiness."""

    assert _state_directory_check(tmp_path) == "ready"

    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        assert _state_directory_check(read_only) == "unavailable"
    finally:
        read_only.chmod(0o700)


def test_a_missing_state_directory_is_unavailable(tmp_path: Path) -> None:
    assert _state_directory_check(tmp_path / "absent") == "unavailable"


def test_the_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    _state_directory_check(tmp_path)

    assert list(tmp_path.iterdir()) == []


# --- broker ---------------------------------------------------------------


def test_a_disabled_broker_is_reported_disabled_not_broken() -> None:
    settings = load_assistant_settings()
    disabled = dataclasses.replace(
        settings, broker=dataclasses.replace(settings.broker, enabled=False)
    )

    assert _broker_check(disabled) == "disabled"


def test_an_absent_broker_socket_is_unavailable(tmp_path: Path) -> None:
    settings = load_assistant_settings()
    enabled = dataclasses.replace(
        settings,
        broker=dataclasses.replace(
            settings.broker, enabled=True, socket_path=tmp_path / "absent.sock"
        ),
    )

    assert _broker_check(enabled) == "unavailable"


def test_a_present_broker_socket_is_ready_without_connecting(tmp_path: Path) -> None:
    """Connecting would activate a root service as a side effect of a probe."""

    import socket as socket_module

    path = tmp_path / "broker.sock"
    listener = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    try:
        settings = load_assistant_settings()
        enabled = dataclasses.replace(
            settings,
            broker=dataclasses.replace(
                settings.broker, enabled=True, socket_path=path
            ),
        )

        assert _broker_check(enabled) == "ready"
        # Nothing connected, so nothing is waiting to be accepted.
        listener.settimeout(0.05)
        with pytest.raises((TimeoutError, OSError)):
            listener.accept()
    finally:
        listener.close()


# --- gating ---------------------------------------------------------------


def test_readiness_gates_on_the_checks_that_stop_the_service_answering() -> None:
    """The broker and the optional capabilities must never gate.

    A beta install deliberately does not provision the broker, and cloud and
    local speech are supported as absent, so gating on them would report a
    working deployment as not ready.
    """

    source = (Path(__file__).resolve().parents[1] / "src" / "butters" / "web" / "app.py").read_text()
    gating = source[source.index("gating = (") : source.index("healthy = all(")]

    assert '"configuration"' in gating
    assert '"state_directory"' in gating
    assert '"deterministic_router"' in gating
    assert '"action_broker"' not in gating
    assert '"cloud_optional"' not in gating
    assert '"local_stt"' not in gating
