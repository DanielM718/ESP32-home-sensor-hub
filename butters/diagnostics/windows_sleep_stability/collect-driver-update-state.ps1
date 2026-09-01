$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$NicDescription = 'Realtek PCIe 5GbE Family Controller'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'driver-update-state.json'
$RelevantDeviceIds = @(
    'PCI\VEN_1022&DEV_15B6&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0341',
    'PCI\VEN_1022&DEV_15B7&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0441',
    'PCI\VEN_1022&DEV_15B8&SUBSYS_7E701462&REV_00\4&5D6807C&0&0043',
    'PCI\VEN_1022&DEV_43FD&SUBSYS_11421B21&REV_01\8&E99A29B&0&006000400011'
)

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

$uninstallRoots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$installedSoftware = @(
    Get-ItemProperty $uninstallRoots -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match '(?i)AMD.*Chipset|Realtek.*Ethernet|Realtek.*Network' } |
        Sort-Object DisplayName, DisplayVersion -Unique |
        Select-Object DisplayName, DisplayVersion, Publisher, InstallDate, UninstallString
)

$signedDrivers = @(
    Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DeviceName -eq $NicDescription -or
            $RelevantDeviceIds -contains $_.DeviceID
        } |
        Sort-Object DeviceID |
        ForEach-Object {
            [ordered]@{
                device_name = [string]$_.DeviceName
                device_id = [string]$_.DeviceID
                device_class = [string]$_.DeviceClass
                driver_provider = [string]$_.DriverProviderName
                driver_version = [string]$_.DriverVersion
                driver_date = [string]$_.DriverDate
                inf_name = [string]$_.InfName
                manufacturer = [string]$_.Manufacturer
                is_signed = [bool]$_.IsSigned
                signer = [string]$_.Signer
            }
        }
)

$windowsUpdateDrivers = @()
$windowsUpdateError = $null
try {
    $session = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $searchResult = $searcher.Search("IsInstalled=0 and Type='Driver'")
    $windowsUpdateDrivers = @(
        foreach ($update in $searchResult.Updates) {
            [ordered]@{
                title = [string]$update.Title
                description = [string]$update.Description
                driver_class = [string]$update.DriverClass
                driver_hardware_id = [string]$update.DriverHardwareID
                driver_manufacturer = [string]$update.DriverManufacturer
                driver_model = [string]$update.DriverModel
                driver_version_date = if ($update.DriverVerDate) { $update.DriverVerDate.ToString('o') } else { $null }
                is_downloaded = [bool]$update.IsDownloaded
                is_mandatory = [bool]$update.IsMandatory
            }
        }
    )
}
catch {
    $windowsUpdateError = [string]$_.Exception.Message
}

$rebootFlags = [ordered]@{
    component_based_servicing = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
    windows_update = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    pending_file_rename = [bool](Get-ItemPropertyValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue)
}

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    boot_time_utc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
    installed_relevant_software = $installedSoftware
    relevant_signed_drivers = $signedDrivers
    available_windows_update_drivers = $windowsUpdateDrivers
    windows_update_search_error = $windowsUpdateError
    reboot_flags = $rebootFlags
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 10 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ([ordered]@{
    hostname = $result.hostname
    collection_time_utc = $result.collection_time_utc
    installed_relevant_software_count = $installedSoftware.Count
    relevant_signed_driver_count = $signedDrivers.Count
    available_windows_update_driver_count = $windowsUpdateDrivers.Count
    windows_update_search_error = $windowsUpdateError
    output_path = $OutputPath
} | ConvertTo-Json -Compress)
