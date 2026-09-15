"""The installer must not delete the shared Desktop Agent protocol package.

`butters_agent.protocol` is the wire contract shared with the Windows Desktop
Agent. It lives in a sibling checkout directory, so the installer's main rsync
of `butters/` does not cover it and it has to be staged explicitly. Production
serves the live agent out of the in-tree copy at /opt/butters/src/butters_agent,
so an installer that omits this step deletes it on the next deployment and takes
the connected agent's backend down with it.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY / "butters/scripts/install-beta1"
AGENT_PACKAGE = REPOSITORY / "butters-agent/src/butters_agent"


def test_installer_refuses_to_run_without_the_shared_agent_package() -> None:
    source = INSTALLER.read_text()
    assert 'agent_package="$(dirname "${butters_dir}")/butters-agent/src/butters_agent"' in source
    assert '[[ -f "${agent_package}/protocol.py" ]]' in source
    assert "Shared agent protocol is missing" in source


def test_installer_stages_the_agent_package_into_the_application_tree() -> None:
    """The staged tree must end up with src/butters_agent beside src/butters."""

    source = INSTALLER.read_text()
    assert '"${agent_package}/" "${staging_dir}/src/butters_agent/"' in source
    # It must be staged after the main rsync, or --delete would remove it again.
    main_rsync = source.index('"${butters_dir}/" "${staging_dir}/"')
    agent_rsync = source.index('"${agent_package}/" "${staging_dir}/src/butters_agent/"')
    assert main_rsync < agent_rsync


def test_installer_shell_syntax_is_valid() -> None:
    assert subprocess.run(
        ["bash", "-n", str(INSTALLER)], capture_output=True, check=False
    ).returncode == 0


@pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is unavailable")
def test_staged_tree_contains_an_importable_agent_protocol(tmp_path) -> None:
    """Reproduce the installer's two staging copies and import from the result.

    This is the property that actually matters: after staging, the exact module
    the compatibility path imports must resolve out of the staged tree alone.
    """

    staging = tmp_path / "staging"
    staging.mkdir()
    # The installer's first rsync, narrowed to src/ so the test does not copy
    # the multi-hundred-megabyte model and runtime directories.
    subprocess.run(
        [
            "rsync", "-a", "--delete",
            "--exclude", "__pycache__", "--exclude", "*.pyc",
            f"{REPOSITORY / 'butters/src'}/", f"{staging / 'src'}/",
        ],
        check=True,
    )
    assert (staging / "src/butters").is_dir()
    assert not (staging / "src/butters_agent").exists(), (
        "the main rsync alone does not provide butters_agent; that is the bug"
    )
    # The installer's second rsync.
    subprocess.run(
        [
            "rsync", "-a", "--delete",
            "--exclude", "__pycache__", "--exclude", "*.pyc",
            f"{AGENT_PACKAGE}/", f"{staging / 'src/butters_agent'}/",
        ],
        check=True,
    )
    assert (staging / "src/butters_agent/protocol.py").is_file()

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import butters_agent.protocol as p;"
                "print(sorted(p.SCHEMAS));"
                "import butters.actions.agent as a;"
                "print(a.AgentHub._protocol().__name__)"
            ),
        ],
        cwd=tmp_path,
        env={"PYTHONPATH": str(staging / "src"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "desktop.app.launch" in probe.stdout
    assert "butters_agent.protocol" in probe.stdout


def test_candidate_hello_validation_rejects_unknown_advertised_actions() -> None:
    """Document why this branch cannot adopt the live agent connection yet.

    `AgentHub._authenticate_hello` requires every action a client advertises to
    exist in this branch's SCHEMAS, and the shipped client advertises
    `sorted(SCHEMAS)` of whatever protocol it was built against. A client built
    against a protocol that also defines `desktop.vm.*` therefore fails
    authentication outright rather than degrading to the actions both sides
    share. That is a deliberate fail-closed choice, but it means migrating a
    production agent to this branch is a coordinated change on both ends, not a
    server-side deployment.
    """

    from butters.actions.agent import AgentHub
    from butters.assistant_config import AgentIngressSettings
    from butters_agent import protocol

    hub = AgentHub(AgentIngressSettings())
    hub._credentials = type(
        "C",
        (),
        {
            "agent_id": "desktop",
            "token_sha256": "0" * 64,
            "key": b"\x00" * 32,
            "protocol_version": 1,
        },
    )()
    token = "a" * 64
    hub._credentials.token_sha256 = hashlib.sha256(token.encode()).hexdigest()

    def hello(actions):
        return {
            "type": "hello",
            "protocol": 1,
            "schema": 1,
            "agent_id": "desktop",
            "version": "1.0.0",
            "token": token,
            "actions": actions,
        }

    # Only-known actions authenticate.
    hub._authenticate_hello(hello(sorted(protocol.SCHEMAS)), protocol.SCHEMAS)

    # One extra advertised action is fatal, not ignored.
    with pytest.raises(Exception) as excinfo:
        hub._authenticate_hello(
            hello([*sorted(protocol.SCHEMAS), "desktop.vm.list"]), protocol.SCHEMAS
        )
    assert "unauthorized" in str(excinfo.value)
