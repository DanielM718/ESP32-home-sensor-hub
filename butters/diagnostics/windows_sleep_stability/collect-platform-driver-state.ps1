$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'platform-driver-state.json'
$Since = (Get-Date).AddHours(-24)
$LogNames = @(
    'Microsoft-Windows-Kernel-Acpi/Diagnostic',
    'Microsoft-Windows-Kernel-Power/Diagnostic',
    'Microsoft-Windows-Kernel-Power/Thermal-Operational',
    'Microsoft-Windows-PCI/Operational',
    'Microsoft-Windows-USB-USBXHCI-Operational',
    'Microsoft-Windows-Kernel-PnP/Configuration',
    'Microsoft-Windows-Kernel-PnP/Device Management',
    'Microsoft-Windows-UserPnp/DeviceInstall'
)

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-LogState {
    param([Parameter(Mandatory = $true)][string]$LogName)
    $log = Get-WinEvent -ListLog $LogName -Force -ErrorAction SilentlyContinue
    if (-not $log) { return [ordered]@{ log_name = $LogName; found = $false } }
    [ordered]@{
        log_name = $LogName
        found = $true
        is_enabled = [bool]$log.IsEnabled
        log_mode = [string]$log.LogMode
        record_count = if ($null -ne $log.RecordCount) { [int64]$log.RecordCount } else { $null }
        maximum_size_bytes = [int64]$log.MaximumSizeInBytes
        last_write_time_utc = if ($log.LastWriteTime) { $log.LastWriteTime.ToUniversalTime().ToString('o') } else { $null }
    }
}

function Convert-EventRecord {
    param([Parameter(Mandatory = $true)]$Event)
    [ordered]@{
        time_created_utc = if ($Event.TimeCreated) { $Event.TimeCreated.ToUniversalTime().ToString('o') } else { $null }
        provider = [string]$Event.ProviderName
        event_id = [int]$Event.Id
        record_id = [int64]$Event.RecordId
        level = [string]$Event.LevelDisplayName
        message = [string]$Event.Message
    }
}

$systemDriver = Get-CimInstance Win32_SystemDriver -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq 'nipcibrd' -or $_.DisplayName -match '(?i)nipcibrd' } |
    Select-Object -First 1
$serviceRegistry = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\nipcibrd' -ErrorAction SilentlyContinue
$binaryPath = $null
if ($systemDriver -and $systemDriver.PathName) {
    $binaryPath = [Environment]::ExpandEnvironmentVariables(([string]$systemDriver.PathName).Trim('"'))
}
$binary = $null
if ($binaryPath -and (Test-Path $binaryPath)) {
    $item = Get-Item $binaryPath
    $signature = Get-AuthenticodeSignature $binaryPath
    $binary = [ordered]@{
        path = $binaryPath
        length = [int64]$item.Length
        creation_time_utc = $item.CreationTimeUtc.ToString('o')
        last_write_time_utc = $item.LastWriteTimeUtc.ToString('o')
        file_version = [string]$item.VersionInfo.FileVersion
        product_version = [string]$item.VersionInfo.ProductVersion
        company_name = [string]$item.VersionInfo.CompanyName
        product_name = [string]$item.VersionInfo.ProductName
        description = [string]$item.VersionInfo.FileDescription
        signature_status = [string]$signature.Status
        signer = if ($signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { $null }
    }
}

$uninstallRoots = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
$matchingSoftware = @(
    Get-ItemProperty $uninstallRoots -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -match '(?i)National Instruments|NI Software|NI Package|PCI Bridge' } |
        Sort-Object DisplayName, DisplayVersion -Unique |
        Select-Object DisplayName, DisplayVersion, Publisher, InstallDate, InstallLocation, UninstallString
)
$events = @(
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'nipcibrd'; StartTime = $Since } -ErrorAction SilentlyContinue |
        Select-Object -First 5000 |
        ForEach-Object { Convert-EventRecord $_ }
)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    system_driver = $systemDriver | Select-Object Name, DisplayName, State, StartMode, PathName, ServiceType, Status, ExitCode
    service_registry = if ($serviceRegistry) {
        $serviceRegistry | Select-Object DisplayName, ImagePath, Start, Type, ErrorControl, Group, Tag
    } else { $null }
    binary = $binary
    matching_installed_software = $matchingSoftware
    relevant_log_states = @($LogNames | ForEach-Object { Get-LogState $_ })
    nipcibrd_events_last_24h = $events
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 10 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ([ordered]@{
    hostname = $result.hostname
    collection_time_utc = $result.collection_time_utc
    nipcibrd_driver_found = [bool]$systemDriver
    nipcibrd_event_count = $events.Count
    output_path = $OutputPath
} | ConvertTo-Json -Compress)
