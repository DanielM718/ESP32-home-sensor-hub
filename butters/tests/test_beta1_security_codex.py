from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from butters.assistant_config import RemediationSettings, WebSettings
from butters.remediation.environment import minimal_codex_environment, sensitive_environment_names
from butters.remediation.codex import CodexCliRemediator
from butters.remediation.model import EngineeringClassification, EngineeringRemediationRequest
from butters.remediation.skill_builder import (
    CodexSkillBuilder,
    SkillAuthoringError,
    _allowed_skill_path,
)
from butters.web.security import AuthPolicy, SecurityError


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    return root


def test_codex_child_environment_excludes_parent_provider_and_deployment_secrets() -> None:
    environment = minimal_codex_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/safe-home",
            "CODEX_HOME": "/safe-codex",
            "OPENAI_API_KEY": "fake-parent-secret",
            "MQTT_PASSWORD": "fake-mqtt-secret",
            "HOME_ASSISTANT_TOKEN": "fake-ha-secret",
            "ADMIN_AUTH_SECRET": "fake-admin-secret",
            "INFLUXDB_TOKEN": "fake-db-secret",
        }
    )

    assert environment == {"PATH": "/usr/bin", "HOME": "/safe-home", "CODEX_HOME": "/safe-codex", "LANG": "C.UTF-8"}
    assert "fake-parent-secret" not in str(environment)


def test_actual_codex_runner_receives_secret_free_environment(tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-parent-secret")
    monkeypatch.setenv("MQTT_PASSWORD", "fake-mqtt-secret")
    observed: list[dict[str, str]] = []

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "git":
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        observed.append(dict(kwargs["env"]))
        return SimpleNamespace(stdout="{}", stderr="", returncode=1)

    settings = RemediationSettings(
        allow_codex_execution=True,
        timeout_seconds=120,
        repository_root=root,
        jobs_dir=tmp_path / "jobs",
    )
    request = EngineeringRemediationRequest(
        "Inspect a bounded Butters issue",
        EngineeringClassification.SOFTWARE_DEFECT,
        "butters",
    )
    CodexCliRemediator(settings, runner=runner, which=lambda _name: "/usr/bin/codex").run(request)

    assert observed
    assert "OPENAI_API_KEY" not in observed[0]
    assert "MQTT_PASSWORD" not in observed[0]
    assert "fake-parent-secret" not in str(observed[0])


def test_skill_builder_allows_observational_restart_history_but_denies_control(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    settings = RemediationSettings(repository_root=root, jobs_dir=tmp_path / "jobs")
    builder = CodexSkillBuilder(settings, tmp_path / "jobs.sqlite3")

    allowed = builder.validate_request(
        "Create a read-only skill that tells me which Butters services have restarted during the last 24 hours."
    )
    assert "read-only" in allowed
    for request in (
        "Create a read-only skill with arbitrary shell command access.",
        "Create a read-only skill that can deploy to production.",
        "Create a read-only skill that can turn on a printer.",
        "Create a read-only skill that reads an API key.",
    ):
        with pytest.raises(SkillAuthoringError):
            builder.validate_request(request)


def test_skill_builder_protects_dirty_base_commit(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    settings = RemediationSettings(repository_root=root, jobs_dir=tmp_path / "jobs")
    builder = CodexSkillBuilder(settings, tmp_path / "jobs.sqlite3")
    (root / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(SkillAuthoringError) as raised:
        builder.submit("Create a read-only skill that reports fixed service restart counts.")

    assert raised.value.code == "dirty_worktree"


@pytest.mark.parametrize(
    ("generated_size", "expected_status"),
    ((200, "patch_ready"), (20_000, "failed")),
)
# The oversized case is now refused by the pre-diff artifact gate, so Git is
# never asked to render the patch into memory.
def test_skill_builder_captures_untracked_patch_and_enforces_diff_bound(
    tmp_path: Path,
    generated_size: int,
    expected_status: str,
) -> None:
    root = _repo(tmp_path)
    settings = RemediationSettings(
        allow_codex_execution=True,
        timeout_seconds=120,
        repository_root=root,
        jobs_dir=tmp_path / "jobs",
        max_patch_bytes=16_384,
    )

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "codex":
            target = Path(str(kwargs["cwd"])) / "butters" / "tests" / "test_generated.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#" * generated_size + "\n", encoding="utf-8")
            return SimpleNamespace(stdout="done", stderr="", returncode=0)
        if command[0].endswith("/python"):
            return SimpleNamespace(stdout="1 passed", stderr="", returncode=0)
        return subprocess.run(command, **kwargs)

    builder = CodexSkillBuilder(
        settings,
        tmp_path / "jobs.sqlite3",
        runner=runner,
        parent_environment={"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    job = builder.submit("Create a read-only skill that reports a bounded fixture observation.")
    result = builder.run(job.job_id)

    assert result.status == expected_status
    if expected_status == "patch_ready":
        assert result.files_changed == ("butters/tests/test_generated.py",)
        assert "test_generated.py" in result.diff
        assert result.tests_passed is True
    else:
        assert result.stopping_reason == "generated_bytes_too_large"
        assert result.diff == ""


def test_skill_builder_refuses_same_process_execution_when_parent_has_secret(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    settings = RemediationSettings(
        allow_codex_execution=True,
        timeout_seconds=120,
        repository_root=root,
        jobs_dir=tmp_path / "jobs",
    )
    parent = {"PATH": "/usr/bin", "OPENAI_API_KEY": "fake-parent-secret"}
    builder = CodexSkillBuilder(
        settings,
        tmp_path / "jobs.sqlite3",
        parent_environment=parent,
    )
    job = builder.submit("Create a read-only skill that reports a bounded fixture observation.")

    with pytest.raises(SkillAuthoringError) as raised:
        builder.run(job.job_id)

    assert raised.value.code == "codex_secret_boundary"
    assert sensitive_environment_names(parent) == ("OPENAI_API_KEY",)
    assert builder.execution_status() == {
        "configured": True,
        "available": False,
        "secret_free_parent": False,
        "automatic_deployment": False,
    }


def test_skill_builder_path_scope_requires_exact_document_names() -> None:
    assert _allowed_skill_path("butters/README.md")
    assert _allowed_skill_path("butters/src/butters/skills/example.py")
    assert not _allowed_skill_path("butters/README.md.evil")
    assert not _allowed_skill_path("butters/src/butters/skills_evil/example.py")


def test_skill_builder_rejects_generated_symlink(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    settings = RemediationSettings(
        allow_codex_execution=True,
        timeout_seconds=120,
        repository_root=root,
        jobs_dir=tmp_path / "jobs",
    )

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[0] == "codex":
            target = Path(str(kwargs["cwd"])) / "butters" / "tests" / "test_generated.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to("/etc/passwd")
            return SimpleNamespace(stdout="done", stderr="", returncode=0)
        return subprocess.run(command, **kwargs)

    builder = CodexSkillBuilder(
        settings,
        tmp_path / "jobs.sqlite3",
        runner=runner,
        parent_environment={"PATH": "/usr/bin", "HOME": str(tmp_path)},
    )
    job = builder.submit("Create a read-only skill that reports a bounded fixture observation.")
    result = builder.run(job.job_id)

    assert result.status == "failed"
    assert result.stopping_reason == "symlink_change_denied"


def test_tailscale_headers_are_trusted_only_for_loopback_and_exact_allowlist() -> None:
    policy = AuthPolicy(WebSettings(admin_identities=("admin@example.com",)))

    assert policy.admin_identity({"tailscale-user-login": "admin@example.com"}, "127.0.0.1") == "admin@example.com"
    with pytest.raises(SecurityError):
        policy.admin_identity({"x-tailscale-user-login": "admin@example.com"}, "127.0.0.1")
    with pytest.raises(SecurityError):
        policy.admin_identity({"tailscale-user-login": "other@example.com"}, "127.0.0.1")
    with pytest.raises(SecurityError):
        policy.admin_identity({"tailscale-user-login": "admin@example.com"}, "100.64.0.10")

    unsafe = AuthPolicy(replace(WebSettings(), host="0.0.0.0", admin_identities=("admin@example.com",)))
    with pytest.raises(SecurityError):
        unsafe.admin_identity({"tailscale-user-login": "admin@example.com"}, "10.0.0.1")
