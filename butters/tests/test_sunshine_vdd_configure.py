from pathlib import Path


def _source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "windows" / "configure-sunshine-vdd.ps1").read_text(
        encoding="utf-8"
    )


def _invoker_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "windows" / "invoke-sunshine-vdd-interactive.ps1").read_text(
        encoding="utf-8"
    )


def _identity_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "windows" / "display-topology.ps1").read_text(encoding="utf-8")


def test_sunshine_vdd_configuration_is_fixed_and_preserves_physical_displays() -> None:
    source = _source()

    assert "DISPLAY94" not in source
    assert "$displayDeviceId = '{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}'" in source
    assert "'Root\\MttVDD'" in source
    assert "'DISPLAY\\MTT1337\\*'" in source
    assert "ChangeDisplaySettingsEx(" not in source
    assert "Get-ButtersValidatedDisplayInventory" in source
    assert "Get-ButtersVirtualDisplay" in source
    assert "[string]$currentVirtual.source_name" in source
    assert "$targetWidth = 2560" in source
    assert "$targetHeight = 1440" in source
    assert "$targetRefresh = 60" in source
    assert "physical_displays_modified = $false" in source
    assert "SetDisplayConfig" not in source
    assert "ensure_only_display" not in source


def test_sunshine_vdd_configuration_pins_capture_and_sdr_policy() -> None:
    source = _source()

    assert "output_name = $displayDeviceId" in source
    assert "dd_configuration_option = 'disabled'" in source
    assert "dd_resolution_option = 'disabled'" in source
    assert "dd_manual_resolution = '2560x1440'" in source
    assert "dd_refresh_rate_option = 'disabled'" in source
    assert "dd_manual_refresh_rate = '60'" in source
    assert "dd_hdr_option = 'disabled'" in source
    assert "dd_config_revert_on_disconnect = 'false'" in source


def test_display_identity_uses_query_display_config_not_display_numbers() -> None:
    source = _identity_source()

    assert "QueryDisplayConfig" in source
    assert "DisplayConfigGetDeviceInfo" in source
    assert "VirtualSunshineId = '{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}'" in source
    assert "VirtualInstance = 'DISPLAY\\MTT1337\\1&15ECD195&4&UID256'" in source
    assert "GigabyteInstance = 'DISPLAY\\GBT270B\\5&19CEA493&0&UID4353'" in source
    assert "OledInstance = 'DISPLAY\\GBT3214\\5&19CEA493&0&UID4357'" in source
    assert "GigabyteEdidManufacturerId = 7252" in source
    assert "GigabyteEdidProductCodeId = 9995" in source
    assert "OledEdidProductCodeId = 12820" in source
    assert "DisplayConfigGetDeviceInfo(source)" in source
    assert "DisplayConfigGetDeviceInfo(target)" in source
    assert "DISPLAY94" not in source


def test_sunshine_vdd_configuration_does_not_restart_or_remove_driver() -> None:
    source = _source().lower()

    assert "restart-service" not in source
    assert "stop-service" not in source
    assert "delete-driver" not in source
    assert "remove-device" not in source
    assert "trustedpublisher" not in source
    assert "shutdown.exe" not in source
    assert "restart-computer" not in source


def test_interactive_invoker_is_a_fixed_bounded_one_shot_task() -> None:
    source = _invoker_source()

    assert "[ValidateSet('Audit', 'Apply')]" in source
    assert "$taskName = 'SunshineVddConfigure'" in source
    assert "$scriptPath = 'C:\\ProgramData\\Butters\\configure-sunshine-vdd.ps1'" in source
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)" in source
    assert ".AddSeconds(45)" in source
    assert "-LogonType Interactive -RunLevel Highest" in source
    assert "Unregister-ScheduledTask" in source
    assert "-Confirm:$false" in source
    assert "Invoke-Expression" not in source
    assert "Start-Process" not in source
