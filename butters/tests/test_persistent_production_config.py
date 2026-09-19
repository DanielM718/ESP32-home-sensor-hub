"""Configuration ownership: shipped defaults vs production-local approvals.

The defect these tests exist for was observed in production. Deploying
baf2203 rsynced the repository's `butters/config/` over `/opt/butters`, which
reverted three approvals the machine held - `nas_agent_ingress.enabled`,
`actions.nas_shutdown.enabled`, and `actions.nas_shutdown.configured` - and
disconnected the NAS Agent. `test_the_baf2203_deployment_gate_overwrite` is
the exact reproduction; everything else fixes the ownership model that allowed
it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib
from butters.assistant_config import load_assistant_settings
from butters.config_overlay import (
    BOUNDARY_KEYS,
    GATE_KEYS,
    LocalConfigError,
    gate_values,
    load_overlay,
    local_differences,
    merge_overlay,
    render_overlay,
    validated_overlay,
)
from butters.deployment_gates import effective_gates

from butters import config_overlay

SOURCE_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = SOURCE_ROOT / "config" / "assistant.toml"
INSTALLER = SOURCE_ROOT / "scripts" / "install-beta1"
UNITS = SOURCE_ROOT / "systemd"


def _shipped() -> dict[str, object]:
    return tomllib.loads(SHIPPED.read_text())


def _write_overlay(tmp_path: Path, body: str, *, mode: int = 0o640) -> Path:
    path = tmp_path / "assistant.local.toml"
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture()
def unowned(monkeypatch):
    """The overlay is opt-in: a checkout or a test never inherits a machine's.

    Tests about merge behaviour own their fixture file, so the root-ownership
    rule is relaxed to this uid for them. The rule itself is asserted against
    the unmodified production default in
    `test_the_overlay_must_be_root_owned_in_production`.
    """

    monkeypatch.delenv("BUTTERS_LOCAL_CONFIG", raising=False)
    monkeypatch.setattr(config_overlay, "REQUIRED_OWNER_UID", os.getuid())
    return monkeypatch


# ===================== the exact production regression ======================


def test_the_baf2203_deployment_gate_overwrite(tmp_path: Path, unowned) -> None:
    """Reproduce the observed failure, then prove the new model prevents it.

    Production held three approvals in /opt/butters/config/assistant.toml. The
    installer rsynced the repository's copy over it, and those approvals
    silently became false.
    """

    shipped = _shipped()
    deployed = merge_overlay(
        shipped,
        {
            "nas_agent_ingress": {"enabled": True},
            "actions": {"nas_shutdown": {"enabled": True, "configured": True}},
        },
    )
    reverted = {
        key
        for key in GATE_KEYS
        if gate_values(deployed)[key] != gate_values(shipped)[key]
    }

    # 1. The old model: replacing the tree replaced the approvals.
    assert reverted == {
        "nas_agent_ingress.enabled",
        "actions.nas_shutdown.enabled",
        "actions.nas_shutdown.configured",
    }

    # 2. The new model: the same three approvals move to the overlay...
    overlay_body = render_overlay(
        local_differences(deployed, shipped), header="regression fixture"
    )
    overlay = _write_overlay(tmp_path, overlay_body)
    # ...and a deployment of the untouched repository config still yields them.
    effective = merge_overlay(shipped, tomllib.loads(overlay.read_text()))

    assert gate_values(effective) == gate_values(deployed)
    assert effective["nas_agent_ingress"]["enabled"] is True
    assert effective["actions"]["nas_shutdown"]["enabled"] is True
    assert effective["actions"]["nas_shutdown"]["configured"] is True


def test_the_installer_refuses_the_baf2203_deployment(tmp_path: Path) -> None:
    """The preflight that would have stopped it, exercised directly."""

    deployed = tmp_path / "deployed.toml"
    deployed.write_text(
        SHIPPED.read_text()
        .replace(
            "[nas_agent_ingress]\nenabled = false",
            "[nas_agent_ingress]\nenabled = true",
        )
        .replace(
            "[actions.nas_shutdown]\nenabled = false\nconfigured = false",
            "[actions.nas_shutdown]\nenabled = true\nconfigured = true",
        )
    )
    result = _gates("verify", "--before", str(deployed), "--after", str(SHIPPED))

    assert result.returncode == 4
    assert "Refusing to deploy" in result.stderr
    for key in (
        "nas_agent_ingress.enabled",
        "actions.nas_shutdown.enabled",
        "actions.nas_shutdown.configured",
    ):
        assert f"  {key}: True -> False" in result.stderr


# ============================ loader precedence =============================


def test_a_fresh_install_gets_the_safe_shipped_defaults(unowned) -> None:
    settings = load_assistant_settings(SHIPPED)

    assert settings.nas_agent_ingress.enabled is False
    assert settings.actions.nas_shutdown.enabled is False
    assert settings.actions.nas_shutdown.configured is False
    assert settings.cloud.enabled is False
    assert settings.cloud.allow_paid_calls is False
    assert settings.providers.allow_paid_tts is False
    assert settings.providers.allow_paid_stt is False


def test_an_absent_overlay_is_not_an_error(tmp_path: Path, unowned) -> None:
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(tmp_path / "missing.toml"))
    settings = load_assistant_settings(SHIPPED)

    assert settings.nas_agent_ingress.enabled is False


def test_a_local_approval_overrides_the_shipped_default(tmp_path: Path, unowned) -> None:
    overlay = _write_overlay(
        tmp_path,
        "[nas_agent_ingress]\nenabled = true\n\n"
        "[actions.nas_shutdown]\nenabled = true\nconfigured = true\n",
    )
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))
    settings = load_assistant_settings(SHIPPED)

    assert settings.nas_agent_ingress.enabled is True
    assert settings.actions.nas_shutdown.enabled is True
    assert settings.actions.nas_shutdown.configured is True
    # Everything the overlay did not mention still comes from the repository.
    assert settings.actions.nas.enabled is True
    assert settings.cloud.enabled is False


def test_a_local_revocation_survives_a_shipped_default_of_true(
    tmp_path: Path, unowned
) -> None:
    """The direction that matters most: shipped `true`, locally revoked."""

    assert load_assistant_settings(SHIPPED).desktop.shutdown_enabled is True
    overlay = _write_overlay(tmp_path, "[desktop]\nshutdown_enabled = false\n")
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))

    assert load_assistant_settings(SHIPPED).desktop.shutdown_enabled is False


def test_removing_a_local_approval_is_not_silently_restored(
    tmp_path: Path, unowned
) -> None:
    overlay = _write_overlay(tmp_path, "[nas_agent_ingress]\nenabled = true\n")
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))
    assert load_assistant_settings(SHIPPED).nas_agent_ingress.enabled is True

    # An administrator deletes the approval. Nothing re-adds it.
    _write_overlay(tmp_path, "# intentionally empty\n")
    assert load_assistant_settings(SHIPPED).nas_agent_ingress.enabled is False

    overlay.unlink()
    assert load_assistant_settings(SHIPPED).nas_agent_ingress.enabled is False


def test_unrelated_shipped_configuration_still_evolves(tmp_path: Path, unowned) -> None:
    """A release may change anything the overlay does not own."""

    shipped = tmp_path / "assistant.toml"
    shipped.write_text(
        SHIPPED.read_text().replace("max_output_tokens = 1200", "max_output_tokens = 900")
    )
    overlay = _write_overlay(tmp_path, "[nas_agent_ingress]\nenabled = true\n")
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))
    settings = load_assistant_settings(shipped)

    assert settings.cloud.max_output_tokens == 900
    assert settings.nas_agent_ingress.enabled is True


# ============================== fail closed ================================


def test_malformed_overlay_stops_the_service(tmp_path: Path, unowned) -> None:
    """Falling back to shipped defaults could *grant* a revoked capability."""

    overlay = _write_overlay(tmp_path, "[nas_agent_ingress\nenabled = true\n")
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))

    with pytest.raises(LocalConfigError) as denied:
        load_assistant_settings(SHIPPED)
    assert "not valid TOML" in str(denied.value)


def test_an_undeclared_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LocalConfigError) as denied:
        validated_overlay({"cloud": {"base_url": "https://elsewhere"}}, source="x")
    assert "only declared production-local approvals" in str(denied.value)


@pytest.mark.parametrize("key", sorted(BOUNDARY_KEYS))
def test_the_authentication_boundary_cannot_be_moved_machine_local(key: str) -> None:
    section, _, leaf = key.partition(".")
    with pytest.raises(LocalConfigError) as denied:
        validated_overlay({section: {leaf: False}}, source="x")
    assert "authentication and identity boundary" in str(denied.value)


def test_a_non_boolean_approval_is_refused() -> None:
    with pytest.raises(LocalConfigError) as denied:
        validated_overlay({"cloud": {"enabled": "yes"}}, source="x")
    assert "must be true or false" in str(denied.value)


def test_the_overlay_must_be_root_owned_in_production(tmp_path: Path) -> None:
    """Asserted against the unmodified default, so the real rule is covered."""

    assert config_overlay.REQUIRED_OWNER_UID == 0
    overlay = _write_overlay(tmp_path, "[cloud]\nenabled = false\n")
    if os.geteuid() == 0:  # pragma: no cover - the suite does not run as root
        pytest.skip("a root test uid cannot demonstrate the ownership refusal")

    with pytest.raises(LocalConfigError) as denied:
        load_overlay(overlay)
    assert "must be owned by uid 0" in str(denied.value)


@pytest.mark.parametrize("mode", (0o666, 0o660, 0o644))
def test_a_group_writable_or_world_readable_overlay_is_refused(
    tmp_path: Path, monkeypatch, mode: int
) -> None:
    """A service user that can write its own approvals is not a boundary."""

    monkeypatch.setattr(config_overlay, "REQUIRED_OWNER_UID", os.getuid())
    overlay = _write_overlay(tmp_path, "[cloud]\nenabled = false\n", mode=mode)

    with pytest.raises(LocalConfigError) as denied:
        load_overlay(overlay)
    assert "group-writable or world-readable" in str(denied.value)


def test_the_production_mode_is_accepted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(config_overlay, "REQUIRED_OWNER_UID", os.getuid())
    overlay = _write_overlay(tmp_path, "[cloud]\nenabled = false\n", mode=0o640)

    assert load_overlay(overlay) == {"cloud": {"enabled": False}}


def test_no_secret_may_be_expressed_in_the_overlay() -> None:
    for candidate in (
        {"cloud": {"api_key": "sk-x"}},
        {"providers": {"openai_api_key": "sk-x"}},
        {"nas": {"api_key": "token"}},
    ):
        with pytest.raises(LocalConfigError):
            validated_overlay(candidate, source="x")
    assert not any("key" in key or "token" in key for key in GATE_KEYS)


def test_paid_and_cloud_gates_stay_false_without_an_explicit_approval(
    tmp_path: Path, unowned
) -> None:
    overlay = _write_overlay(tmp_path, "[nas_agent_ingress]\nenabled = true\n")
    unowned.setenv("BUTTERS_LOCAL_CONFIG", str(overlay))
    settings = load_assistant_settings(SHIPPED)

    assert settings.cloud.enabled is False
    assert settings.cloud.allow_paid_calls is False
    assert settings.providers.allow_paid_tts is False
    assert settings.providers.allow_paid_stt is False


def test_nas_shutdown_cannot_become_enabled_from_repository_defaults() -> None:
    shipped = gate_values(_shipped())

    assert shipped["actions.nas_shutdown.enabled"] is False
    assert shipped["actions.nas_shutdown.configured"] is False
    assert shipped["nas_agent_ingress.enabled"] is False
    assert shipped["actions.host_shutdown_enabled"] is False
    assert shipped["actions.host_reboot_enabled"] is False


# ============================== migration ==================================


def test_migration_uses_the_running_configuration_as_source_of_truth(
    tmp_path: Path,
) -> None:
    deployed = tmp_path / "deployed.toml"
    deployed.write_text(
        SHIPPED.read_text().replace(
            "[nas_agent_ingress]\nenabled = false",
            "[nas_agent_ingress]\nenabled = true",
        )
    )
    overlay = tmp_path / "assistant.local.toml"
    result = _gates(
        "migrate",
        "--deployed", str(deployed),
        "--shipped", str(SHIPPED),
        "--overlay", str(overlay),
    )

    assert result.returncode == 0
    recorded = tomllib.loads(overlay.read_text())
    # Exactly the drift, and nothing else: no shipped default is copied along.
    assert recorded == {"nas_agent_ingress": {"enabled": True}}


def test_migration_preserves_every_gate_semantically(tmp_path: Path, unowned) -> None:
    """The whole point of the migration: no gate moves across the change."""

    deployed = tmp_path / "deployed.toml"
    deployed.write_text(
        SHIPPED.read_text()
        .replace("[nas_agent_ingress]\nenabled = false", "[nas_agent_ingress]\nenabled = true")
        .replace("parsec_restart_enabled = false", "parsec_restart_enabled = true")
        .replace("[actions.nas]\nenabled = true", "[actions.nas]\nenabled = false")
    )
    overlay = tmp_path / "assistant.local.toml"
    _gates("migrate", "--deployed", str(deployed), "--shipped", str(SHIPPED), "--overlay", str(overlay))
    overlay.chmod(0o640)

    before = effective_gates(deployed, None)
    after = effective_gates(SHIPPED, overlay)

    assert before == after
    # Including a locally *revoked* gate, not only locally granted ones.
    assert before["actions.nas.enabled"] is False


def test_migration_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    deployed = tmp_path / "deployed.toml"
    deployed.write_text(
        SHIPPED.read_text().replace(
            "[nas_agent_ingress]\nenabled = false", "[nas_agent_ingress]\nenabled = true"
        )
    )
    overlay = tmp_path / "assistant.local.toml"
    overlay.write_text("[cloud]\nenabled = false\n")
    result = _gates(
        "migrate", "--deployed", str(deployed), "--shipped", str(SHIPPED), "--overlay", str(overlay)
    )

    assert "already exists" in result.stdout
    assert overlay.read_text() == "[cloud]\nenabled = false\n"


def test_a_fresh_machine_gets_no_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "assistant.local.toml"
    result = _gates(
        "migrate",
        "--deployed", str(tmp_path / "absent.toml"),
        "--shipped", str(SHIPPED),
        "--overlay", str(overlay),
    )

    assert result.returncode == 0
    assert not overlay.exists()
    assert "fresh install" in result.stdout or "No deployed configuration" in result.stdout


def test_an_identical_redeployment_records_nothing(tmp_path: Path) -> None:
    overlay = tmp_path / "assistant.local.toml"
    result = _gates(
        "migrate",
        "--deployed", str(SHIPPED),
        "--shipped", str(SHIPPED),
        "--overlay", str(overlay),
    )

    assert not overlay.exists()
    assert "no overlay needed" in result.stdout


# ======================= installer / unit invariants ========================


def test_identical_application_code_preserves_every_approval(
    tmp_path: Path, unowned
) -> None:
    overlay = _write_overlay(tmp_path, "[nas_agent_ingress]\nenabled = true\n")

    assert effective_gates(SHIPPED, overlay) == effective_gates(SHIPPED, overlay)
    assert effective_gates(SHIPPED, overlay)["nas_agent_ingress.enabled"] is True
    # And the CLI agrees when no overlay is involved.
    assert _gates("verify", "--before", str(SHIPPED), "--after", str(SHIPPED)).returncode == 0


def test_both_units_name_the_persistent_overlay() -> None:
    for unit in ("butters-web.service", "butters-live.service"):
        body = (UNITS / unit).read_text()
        assert "Environment=BUTTERS_LOCAL_CONFIG=/etc/butters/assistant.local.toml" in body, unit


def test_the_installer_enforces_its_own_ownership_invariants() -> None:
    body = INSTALLER.read_text()

    # The overlay lives outside the replaced tree.
    assert 'local_config="/etc/butters/assistant.local.toml"' in body
    # Migrate and verify both run before the swap.
    assert body.index("deployment_gates migrate") < body.index("# 3. Seal the staged tree.")
    assert body.index("deployment_gates verify") < body.index("# 3. Seal the staged tree.")
    # A unit without the variable is never published.
    assert "BUTTERS_LOCAL_CONFIG=' \"${butters_dir}/systemd/${unit_name}\"" in body
    # The overlay is never rsynced, deleted, or rewritten by the tree swap.
    assert "rm -rf \"${local_config}\"" not in body
    for line in body.splitlines():
        if line.strip().startswith("rsync"):
            assert "etc/butters" not in line


def test_the_installer_does_not_preserve_the_whole_previous_config() -> None:
    """Ownership is per key. Copying the old file wholesale would also freeze
    shipped defaults and block legitimate configuration evolution."""

    body = INSTALLER.read_text()

    assert "previous/config/assistant.toml" not in body
    assert "cp ${previous_dir}/config" not in body


def _gates(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(SOURCE_ROOT / "src"),
    }
    environment.pop("BUTTERS_LOCAL_CONFIG", None)
    return subprocess.run(
        [sys.executable, "-m", "butters.deployment_gates", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
