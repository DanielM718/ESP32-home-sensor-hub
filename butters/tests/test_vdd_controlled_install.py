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
    assert "Windows explicitly reports the installed driver as unsigned" not in source
    assert "DEVPKEY_Device_DriverInfPath" in source
    assert "signed_driver_record_available = $signedDriver.Count -ge 1" in source
    assert "signed_driver_metadata_only = $true" in source
    assert "package_signature_validated = $packageSignatureValidated" in source
    assert "Driver package catalog is valid" in source
    assert "Signer Name\\s*=\\s*SignPath Foundation" in source
    assert "CimSignatureState = 'missing'" in source
    assert "name='cim_reports_false'" in source
    assert "name='cim_missing'" in source
    assert "name='signature_metadata_unavailable'" in source
    assert "[ValidateSet('valid','invalid','unknown')]" in source


def test_vdd_failure_injection_covers_every_diagnostic_retention_case() -> None:
    source = _installer_source()

    retained_cases = (
        "cim_reports_false",
        "cim_missing",
        "signature_metadata_unavailable",
        "reboot_required",
        "display_guid_delayed",
        "sunshine_enumeration_failed",
        "sunshine_capture_failed",
        "optional_tool_exception",
        "virtual_mode_incomplete",
        "child_delayed",
    )
    for case in retained_cases:
        assert f"name='{case}'" in source

    rollback_cases = (
        "signature_invalid",
        "package_missing",
        "root_missing",
        "root_problem",
        "wrong_root_driver",
        "access_regression",
    )
    for case in rollback_cases:
        assert f"name='{case}'" in source

    assert "if (-not $passed) { throw" in source
    assert "policy_self_test_passed=$true" in source


def test_vdd_uninstall_is_reachable_only_through_confirmed_policy_rollback() -> None:
    source = _installer_source()

    # There is one executable device removal and one executable package
    # removal. The other occurrences are text-only recovery instructions.
    assert source.count("& $nefcon --remove-device-node") == 1
    assert source.count("& pnputil.exe /delete-driver") == 1
    assert source.count("Invoke-ConfirmedUnsafeRollback") == 3  # definition + two guarded calls
    assert "Invoke-Rollback" not in source
    assert "$mutationStarted" not in source
    assert "if ($disposition.rollback)" in source
    assert "if ($catchDisposition.rollback)" in source


def test_vdd_downstream_display_and_sunshine_state_cannot_authorize_rollback() -> None:
    source = _installer_source()
    policy = source.split("function Get-InstallDisposition", 1)[1].split(
        "function Invoke-PolicyFailureInjectionTests", 1
    )[0]

    for diagnostic in (
        "$CimSignatureState",
        "$RebootRequested",
        "$ChildHealthy",
        "$VirtualMonitorActive",
        "$DisplayGuidDiscovered",
        "$SunshineEnumerated",
        "$SunshineCaptureReady",
        "$OptionalValidationFailed",
        "$VirtualModeConfigured",
    ):
        assert policy.count(diagnostic) == 1  # declaration only

    # Installation and downstream Sunshine integration are separate phases.
    assert "Restart-Service SunshineService" not in source
    assert "output_name =" not in source
    assert "switch.desktop_gigabyte" not in source
    assert "switch.desktop_oled" not in source


def test_vdd_installer_never_reboots_or_weakens_signature_enforcement() -> None:
    source = _installer_source().lower()

    assert "shutdown.exe /r" not in source
    assert "restart-computer" not in source
    assert "testsigning" not in source
    assert "nointegritychecks" not in source
    assert "cert:\\localmachine\\root" in source
    assert "import-certificate -filepath $certificatepath -certstorelocation cert:\\localmachine\\trustedpublisher" in source
