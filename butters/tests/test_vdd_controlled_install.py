from pathlib import Path


def _installer_source() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "windows" / "vdd-controlled-install.ps1").read_text(
        encoding="utf-8"
    )


def test_vdd_validator_keeps_package_root_child_and_active_display_separate() -> None:
    source = _installer_source()

    assert "Get-PnpDevice -Class Display -PresentOnly:$false" not in source
    assert "(Get-HardwareIds $_) -contains 'Root\\MttVDD'" in source
    assert "$_.InstanceId -like 'DISPLAY\\MTT1337\\*'" in source
    assert "$_.Active -and $_.InstanceName -like 'DISPLAY\\MTT1337\\*'" in source
    assert "package_published = [bool]$publishedInf" in source
    assert "root_started = $rootHealthy" in source
    assert "child_started = $childHealthy" in source
    assert "virtual_monitor_active = $activeChildren.Count -ge 1" in source


def test_vdd_validator_does_not_equate_delayed_cim_metadata_with_unsigned() -> None:
    source = _installer_source()

    assert "Signed driver record is absent or unsigned" not in source
    assert "DEVPKEY_Device_DriverInfPath" in source
    assert "Windows explicitly reports the installed driver as unsigned" in source
    assert "signed_driver_record_available = $signedDriver.Count -ge 1" in source
    assert "$retainInstalledDriver" in source
    assert "if (-not $retainInstalledDriver) { Invoke-Rollback }" in source


def test_vdd_installer_never_reboots_or_weakens_signature_enforcement() -> None:
    source = _installer_source().lower()

    assert "shutdown.exe /r" not in source
    assert "restart-computer" not in source
    assert "testsigning" not in source
    assert "nointegritychecks" not in source
    assert "cert:\\localmachine\\root" in source
    assert "import-certificate -filepath $certificatepath -certstorelocation cert:\\localmachine\\trustedpublisher" in source
