$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'amd-chipset-components.json'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

$uninstallRoots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$installedSoftware = @(
    Get-ItemProperty $uninstallRoots -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match '(?i)AMD.*Chipset|AMD.*PCI|AMD.*GPIO|AMD.*PSP|AMD.*SMBus|AMD.*I2C' } |
        Sort-Object DisplayName, DisplayVersion -Unique |
        Select-Object DisplayName, DisplayVersion, Publisher, InstallDate, InstallLocation, UninstallString
)

$presentIds = @{}
foreach ($device in @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)) {
    $presentIds[[string]$device.InstanceId] = $true
}
$amdDrivers = @(
    Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DriverProviderName -match '(?i)^AMD$|Advanced Micro Devices' -or
            $_.Manufacturer -match '(?i)^AMD$|Advanced Micro Devices'
        } |
        Sort-Object DeviceName, DriverVersion, DeviceID |
        ForEach-Object {
            [ordered]@{
                device_name = [string]$_.DeviceName
                device_id = [string]$_.DeviceID
                present = $presentIds.ContainsKey([string]$_.DeviceID)
                device_class = [string]$_.DeviceClass
                driver_provider = [string]$_.DriverProviderName
                driver_version = [string]$_.DriverVersion
                driver_date = [string]$_.DriverDate
                inf_name = [string]$_.InfName
                manufacturer = [string]$_.Manufacturer
                signer = [string]$_.Signer
            }
        }
)

$xHciDrivers = @(
    Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
        Where-Object {
            $_.DeviceName -match '(?i)eXtensible Host Controller' -or
            $_.DeviceID -match '(?i)VEN_1022&DEV_(15B6|15B7|15B8|43FD)'
        } |
        Sort-Object DeviceID |
        ForEach-Object {
            [ordered]@{
                device_name = [string]$_.DeviceName
                device_id = [string]$_.DeviceID
                present = $presentIds.ContainsKey([string]$_.DeviceID)
                driver_provider = [string]$_.DriverProviderName
                driver_version = [string]$_.DriverVersion
                driver_date = [string]$_.DriverDate
                inf_name = [string]$_.InfName
            }
        }
)

$cacheRoot = 'C:\AMD\Chipset_Software\Packages\IODriver'
$cachedPackages = @()
if (Test-Path $cacheRoot) {
    $cachedPackages = @(
        Get-ChildItem $cacheRoot -Directory -Recurse -Depth 3 -ErrorAction SilentlyContinue |
            Sort-Object FullName |
            ForEach-Object { [string]$_.FullName }
    )
}

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    installed_amd_software = $installedSoftware
    amd_signed_drivers = $amdDrivers
    xhci_signed_drivers = $xHciDrivers
    cached_iodriver_directories = $cachedPackages
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 10 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ([ordered]@{
    hostname = $result.hostname
    collection_time_utc = $result.collection_time_utc
    installed_amd_software_count = $installedSoftware.Count
    amd_signed_driver_count = $amdDrivers.Count
    xhci_signed_driver_count = $xHciDrivers.Count
    cached_iodriver_directory_count = $cachedPackages.Count
    output_path = $OutputPath
} | ConvertTo-Json -Compress)
