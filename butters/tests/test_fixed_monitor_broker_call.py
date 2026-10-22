from pathlib import Path


def _source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "scripts" / "fixed-monitor-broker-call").read_text(
        encoding="utf-8"
    )


def test_monitor_recovery_helper_exposes_only_existing_fixed_operations() -> None:
    source = _source()

    assert '"off": "desktop.monitors_off"' in source
    assert '"on": "desktop.monitors_on"' in source
    assert "SOCKET_PATH = \"/run/butters-action-broker/broker.sock\"" in source
    assert "request_id" in source
    assert "error_code" in source
    assert "subprocess" not in source
    assert "eval(" not in source
    assert "exec(" not in source
    assert "home_assistant" not in source.lower()


def test_monitor_recovery_helper_is_bounded() -> None:
    source = _source()

    assert "connection.settimeout(75)" in source
    assert "MAX_RESPONSE_BYTES = 64 * 1024" in source
    assert "if len(sys.argv) != 2 or sys.argv[1] not in OPERATIONS" in source
