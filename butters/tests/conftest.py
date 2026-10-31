from __future__ import annotations

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE_ROOT))


import pytest


@pytest.fixture(autouse=True)
def _untrusted_clock_by_default(monkeypatch, tmp_path_factory):
    """No test depends on this machine's NTP state.

    The KR260 recovery clock check reads timesyncd's marker. Point it at a
    path that never exists, so a test that needs a trusted clock says so.
    """

    from butters.integrations import clock_trust

    missing = tmp_path_factory.getbasetemp() / "no-timesync-marker"
    monkeypatch.setattr(clock_trust, "MARKER", missing)
