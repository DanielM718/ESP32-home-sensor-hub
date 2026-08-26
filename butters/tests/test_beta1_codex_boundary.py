"""Codex authoring boundary: job state, artifact bounds, approval revalidation."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from butters.assistant_config import RemediationSettings
from butters.remediation.skill_builder import (
    CodexSkillBuilder,
    SkillAuthoringError,
    _allowed_skill_path,
    _patch_violation,
)


SAFE_REQUEST = "Create a read-only skill that reports a bounded fixture observation."
SAFE_PARENT = {"PATH": "/usr/bin", "HOME": "/nonexistent"}


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


def _settings(tmp_path: Path, root: Path, **overrides: object) -> RemediationSettings:
    values: dict[str, object] = {
        "allow_codex_execution": True,
        "timeout_seconds": 120,
        "repository_root": root,
        "jobs_dir": tmp_path / "jobs",
    }
    values.update(overrides)
    return RemediationSettings(**values)  # type: ignore[arg-type]


def _generator(relative: str, content: bytes, *, mode: int | None = None):
    def runner(command: list[str], **kwargs: object):
        if command[0] == "codex":
            target = Path(str(kwargs["cwd"])) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if mode is not None:
                target.chmod(mode)
            return SimpleNamespace(stdout="done", stderr="", returncode=0)
        if command[0].endswith("/python"):
            return SimpleNamespace(stdout="1 passed", stderr="", returncode=0)
        return subprocess.run(command, **kwargs)

    return runner


def test_concurrent_run_of_one_job_produces_exactly_one_execution(tmp_path: Path) -> None:
    """M-9: queued -> running is a compare-and-swap, so /run cannot double-execute."""

    root = _repo(tmp_path)
    executions: list[str] = []
    barrier = threading.Barrier(2, timeout=10)

    def runner(command: list[str], **kwargs: object):
        if command[0] == "codex":
            executions.append("codex")
            target = Path(str(kwargs["cwd"])) / "butters" / "tests" / "test_generated.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# generated\n", encoding="utf-8")
            return SimpleNamespace(stdout="done", stderr="", returncode=0)
        if command[0].endswith("/python"):
            return SimpleNamespace(stdout="1 passed", stderr="", returncode=0)
        return subprocess.run(command, **kwargs)

    builder = CodexSkillBuilder(
        _settings(tmp_path, root),
        tmp_path / "jobs.sqlite3",
        runner=runner,
        parent_environment=SAFE_PARENT,
    )
    job = builder.submit(SAFE_REQUEST)
    outcomes: list[object] = []

    def attempt() -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        try:
            outcomes.append(builder.run(job.job_id))
        except SkillAuthoringError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(executions) == 1, "the job must execute exactly once"
    conflicts = [item for item in outcomes if isinstance(item, SkillAuthoringError)]
    assert len(conflicts) == 1
    assert conflicts[0].code in {"job_already_running", "invalid_job_state"}


def test_rejected_run_returns_the_job_to_a_retryable_state(tmp_path: Path) -> None:
    """M-9: a refused attempt must not wedge the job in `running` forever."""

    root = _repo(tmp_path)
    builder = CodexSkillBuilder(
        _settings(tmp_path, root),
        tmp_path / "jobs.sqlite3",
        parent_environment=SAFE_PARENT,
    )
    job = builder.submit(SAFE_REQUEST)
    (root / "README.md").write_text("now dirty\n", encoding="utf-8")

    with pytest.raises(SkillAuthoringError) as denied:
        builder.run(job.job_id)
    assert denied.value.code == "dirty_worktree"
    assert builder.require(job.job_id).status == "queued"

    # Once the tree is clean again the same job can be retried.
    subprocess.run(["git", "checkout", "--", "README.md"], cwd=root, check=True)
    assert builder.require(job.job_id).status == "queued"


def test_leftover_job_directory_does_not_wedge_a_retry(tmp_path: Path) -> None:
    """M-9: a partially created attempt directory must not be permanent."""

    root = _repo(tmp_path)
    settings = _settings(tmp_path, root)
    (settings.jobs_dir).mkdir(parents=True, exist_ok=True)
    builder = CodexSkillBuilder(
        settings,
        tmp_path / "jobs.sqlite3",
        runner=_generator("butters/tests/test_generated.py", b"# generated\n"),
        parent_environment=SAFE_PARENT,
    )
    job = builder.submit(SAFE_REQUEST)
    (settings.jobs_dir / job.job_id).mkdir(parents=True, exist_ok=True)
    (settings.jobs_dir / job.job_id / "worktree").mkdir()

    result = builder.run(job.job_id)
    assert result.status == "patch_ready"


@pytest.mark.parametrize(
    ("kind", "expected"),
    (
        ("oversized", "generated_file_too_large"),
        ("binary", "binary_artifact_denied"),
        ("executable", "executable_artifact_denied"),
    ),
)
def test_generated_artifacts_are_bounded_before_the_diff_is_captured(
    tmp_path: Path, kind: str, expected: str
) -> None:
    """L-6: reject oversized/binary/executable output before Git renders it."""

    # Built here rather than as a parameter: pytest exports the parameter in
    # PYTEST_CURRENT_TEST, and a large value would overflow the child environment.
    artifacts = {
        "oversized": ("butters/tests/test_big.py", b"#" * 300_000, None),
        "binary": ("butters/tests/test_binary.py", b"\x00\x01\x02binary", None),
        "executable": ("butters/tests/test_exec.py", b"# generated\n", 0o755),
    }
    relative, content, mode = artifacts[kind]

    root = _repo(tmp_path)
    diffed: list[str] = []

    inner = _generator(relative, content, mode=mode)

    def runner(command: list[str], **kwargs: object):
        if command[0] == "git" and len(command) > 1 and command[1] == "diff" and "--binary" in command:
            diffed.append(" ".join(command))
        return inner(command, **kwargs)

    builder = CodexSkillBuilder(
        _settings(tmp_path, root, max_generated_file_bytes=65_536),
        tmp_path / "jobs.sqlite3",
        runner=runner,
        parent_environment=SAFE_PARENT,
    )
    job = builder.submit(SAFE_REQUEST)
    result = builder.run(job.job_id)

    assert result.status == "failed"
    assert result.stopping_reason == expected
    assert result.diff == ""
    assert not diffed, "the patch must never be rendered for a rejected artifact"


def test_approval_revalidates_every_path_in_the_stored_patch(tmp_path: Path) -> None:
    """L-7: the persisted diff is untrusted input again at approval time."""

    root = _repo(tmp_path)
    builder = CodexSkillBuilder(
        _settings(tmp_path, root),
        tmp_path / "jobs.sqlite3",
        runner=_generator("butters/tests/test_generated.py", b"# generated\n"),
        parent_environment=SAFE_PARENT,
    )
    job = builder.submit(SAFE_REQUEST)
    ready = builder.run(job.job_id)
    assert ready.status == "patch_ready"

    # Simulate post-validation tampering of the stored patch.
    escaped = ready.diff.replace(
        "butters/tests/test_generated.py", "butters/systemd/butters-web.service"
    )
    builder._save(_with_diff(ready, escaped))

    with pytest.raises(SkillAuthoringError) as denied:
        builder.approve(job.job_id)
    assert denied.value.code == "path_scope_denied"
    # Nothing was applied to the real repository.
    assert not (root / "butters" / "systemd").exists()


def _with_diff(job, diff: str):
    from butters.remediation.skill_builder import replace_job

    return replace_job(job, diff=diff)


@pytest.mark.parametrize(
    ("diff", "expected"),
    (
        ("diff --git a/etc/passwd b/etc/passwd\n--- a/etc/passwd\n+++ b/etc/passwd\n", "path_scope_denied"),
        (
            "diff --git a/butters/tests/x.py b/butters/tests/x.py\ndeleted file mode 100644\n"
            "--- a/butters/tests/x.py\n+++ /dev/null\n",
            "destructive_change_denied",
        ),
        (
            "diff --git a/butters/tests/x b/butters/tests/x\nnew file mode 120000\n"
            "--- /dev/null\n+++ b/butters/tests/x\n",
            "symlink_change_denied",
        ),
        (
            "diff --git a/butters/tests/x.py b/butters/tests/x.py\nnew file mode 100755\n"
            "--- /dev/null\n+++ b/butters/tests/x.py\n",
            "unsupported_mode_denied",
        ),
        (
            "diff --git a/butters/tests/x.png b/butters/tests/x.png\nGIT binary patch\nliteral 5\n",
            "binary_patch_denied",
        ),
        (
            "diff --git a/butters/tests/a.py b/butters/tests/b.py\nrename from butters/tests/a.py\n"
            "rename to butters/tests/b.py\n",
            "rename_or_copy_denied",
        ),
        ("", "unparsable_patch"),
    ),
)
def test_patch_revalidation_rejects_every_out_of_scope_shape(diff: str, expected: str) -> None:
    assert _patch_violation(diff, 512 * 1024) == expected


def test_patch_revalidation_accepts_a_normal_in_scope_skill_patch() -> None:
    diff = (
        "diff --git a/butters/src/butters/skills/new_skill.py b/butters/src/butters/skills/new_skill.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/butters/src/butters/skills/new_skill.py\n"
        "@@ -0,0 +1 @@\n+# generated\n"
    )
    assert _patch_violation(diff, 512 * 1024) is None


def test_path_policy_still_rejects_traversal_and_prefix_lookalikes() -> None:
    assert _allowed_skill_path("butters/tests/test_ok.py")
    assert not _allowed_skill_path("butters/tests/../../../etc/passwd")
    assert not _allowed_skill_path("/etc/passwd")
    assert not _allowed_skill_path("butters/tests_evil/test.py")
    assert not _allowed_skill_path("butters/src/butters/web/app.py")


def test_unreadable_repository_root_is_a_typed_failure_not_a_crash(tmp_path: Path) -> None:
    """M-6: a deployment without a readable checkout must not return HTTP 500."""

    builder = CodexSkillBuilder(
        _settings(tmp_path, tmp_path / "definitely-absent"),
        tmp_path / "jobs.sqlite3",
        parent_environment=SAFE_PARENT,
    )
    with pytest.raises(SkillAuthoringError) as denied:
        builder.submit(SAFE_REQUEST)
    assert denied.value.code == "repository_unavailable"


def test_codex_execution_stays_disabled_by_default() -> None:
    """H-3 is deferred: programmatic execution must remain off until sandboxed."""

    assert RemediationSettings().allow_codex_execution is False
