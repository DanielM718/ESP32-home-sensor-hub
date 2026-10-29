"""Read-only FPGA (PL) telemetry in the KR260 observation."""

from __future__ import annotations

import re

import pytest
from test_kr260_management import TELEMETRY, Broker, Runner, settings

from butters.actions.broker import _KR260_TELEMETRY_COMMAND
from butters.integrations.kr260 import Kr260Observer

STARTER = "1e998ffd5d33a05da3746df3603e65603e1628006bc878209a836cc9bd3c4ae1"
# Captured read-only from the board on 2026-10-29.
FPGA_LINES = f"""fpga_state=operating
fpga_status=
fpga_overlay=k26-starter-kits_image_1 applied {STARTER}
"""


def observe(extra: str) -> dict[str, object]:
    observer = Kr260Observer(
        settings(None), broker=Broker(output=TELEMETRY + extra), runner=Runner()
    )
    return observer.observe(refresh=True)


def test_the_starter_kit_is_recognized_by_its_overlay_hash():
    fpga = observe(FPGA_LINES)["fpga"]
    assert fpga == {
        "state": "operating",
        "manager_state": "operating",
        "status": "",
        "overlays": [
            {
                "name": "k26-starter-kits_image_1",
                "status": "applied",
                "sha256": STARTER,
                "design": "k26-starter-kits",
            }
        ],
        "design": "k26-starter-kits",
        "starter_kit_loaded": True,
    }


def test_older_telemetry_without_fpga_lines_is_unavailable_not_an_error():
    observed = observe("")
    assert observed["identity_verified"] is True
    assert observed["fpga"]["state"] == "unavailable"
    assert observed["fpga"]["starter_kit_loaded"] is False


@pytest.mark.parametrize(
    "lines, state, loaded",
    [
        ("fpga_state=operating\nfpga_status=\n", "operating_without_overlay", False),
        ("fpga_state=unknown\nfpga_status=\n", "unconfigured", False),
        ("fpga_state=write error\nfpga_status=\n", "error", False),
        (
            f"fpga_state=operating\nfpga_status=reconfig operation error\nfpga_overlay=x applied {STARTER}\n",
            "error",
            True,
        ),
        (
            f"fpga_state=operating\nfpga_status=\nfpga_overlay=custom applied {'a' * 64}\n",
            "unreviewed_design",
            False,
        ),
        (
            "fpga_state=operating\nfpga_status=\nfpga_overlay=custom applied -\n",
            "unreviewed_design",
            False,
        ),
        (
            f"fpga_state=operating\nfpga_status=\nfpga_overlay=x unapplied {STARTER}\n",
            "operating_without_overlay",
            False,
        ),
        ("fpga_state=on fire\nfpga_status=\n", "unrecognized", False),
        ("fpga_state=\nfpga_status=\n", "unavailable", False),
    ],
)
def test_fpga_states(lines, state, loaded):
    fpga = observe(lines)["fpga"]
    assert fpga["state"] == state
    assert fpga["starter_kit_loaded"] is loaded


def test_overlay_records_are_bounded_and_shape_checked():
    flood = "".join(f"fpga_overlay=o{i} applied {STARTER}\n" for i in range(20))
    junk = "fpga_overlay=../../etc applied x\nfpga_overlay=a b c d\n"
    fpga = observe("fpga_state=operating\nfpga_status=\n" + junk + flood)["fpga"]
    assert len(fpga["overlays"]) == 4
    assert all(item["name"].startswith("o") for item in fpga["overlays"])


def test_unexpected_status_text_is_not_passed_through():
    fpga = observe("fpga_state=operating\nfpga_status=<script>\n")["fpga"]
    assert fpga["status"] == "unrecognized"
    assert fpga["state"] == "error"


def test_fpga_lines_in_the_fixed_command_only_read():
    start = _KR260_TELEMETRY_COMMAND.index("fpga_state=")
    fpga = _KR260_TELEMETRY_COMMAND[start:]
    # No writes, no programming tools, no privilege.
    assert ">" not in fpga.replace("2>/dev/null", "")
    for word in (
        "echo",
        "tee",
        "xmutil",
        "fpgautil",
        "dfx-mgr",
        "sudo",
        "rm ",
        "mkdir",
        "dd ",
    ):
        assert word not in fpga
    # Only these paths are touched.
    paths = set(re.findall(r"/(?:sys|lib|proc|etc|dev)[^\s\"');]*", fpga))
    assert paths <= {
        "/sys/class/fpga_manager/fpga0/state",
        "/sys/class/fpga_manager/fpga0/status",
        "/sys/kernel/config/device-tree/overlays/*/",
        "/lib/firmware/$p",
        "/dev/null",
    }
