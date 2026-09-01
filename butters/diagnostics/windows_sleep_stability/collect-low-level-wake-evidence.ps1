$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'low-level-wake-evidence.json'
$Since = (Get-Date).AddHours(-6)
$RelevantNamePattern = '(?i)acpi|pci|power|sleep|wake|resume|usb|xhci|pnp|ndis|network|driverframework|taskscheduler|windowsupdate|maintenance'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Convert-EventRecord {
    param([Parameter(Mandatory = $true)]$Event)

    $eventData = [ordered]@{}
    try {
        [xml]$xml = $Event.ToXml()
        $index = 0
        foreach ($node in @($xml.Event.EventData.Data)) {
            $name = [string]$node.Name
            if ([string]::IsNullOrWhiteSpace($name)) { $name = "index_$index" }
            $eventData[$name] = [string]$node.'#text'
            $index++
        }
    }
    catch {
        $eventData['_parse_error'] = [string]$_.Exception.Message
    }

    [ordered]@{
        time_created = if ($Event.TimeCreated) { $Event.TimeCreated.ToString('o') } else { $null }
        time_created_utc = if ($Event.TimeCreated) { $Event.TimeCreated.ToUniversalTime().ToString('o') } else { $null }
        provider = [string]$Event.ProviderName
        log_name = [string]$Event.LogName
        event_id = [int]$Event.Id
        record_id = [int64]$Event.RecordId
        level = [string]$Event.LevelDisplayName
        message = [string]$Event.Message
        event_data = $eventData
    }
}

function Convert-LogMetadata {
    param([Parameter(Mandatory = $true)]$Log)

    [ordered]@{
        log_name = [string]$Log.LogName
        is_enabled = [bool]$Log.IsEnabled
        log_mode = [string]$Log.LogMode
        record_count = if ($null -ne $Log.RecordCount) { [int64]$Log.RecordCount } else { $null }
        maximum_size_bytes = [int64]$Log.MaximumSizeInBytes
        last_write_time = if ($Log.LastWriteTime) { $Log.LastWriteTime.ToString('o') } else { $null }
    }
}

function Convert-ProviderMetadata {
    param([Parameter(Mandatory = $true)]$Provider)

    [ordered]@{
        name = [string]$Provider.Name
        log_links = @($Provider.LogLinks | ForEach-Object { [string]$_.LogName })
    }
}

$allLogs = @(Get-WinEvent -ListLog * -ErrorAction SilentlyContinue)
$relevantLogs = @(
    $allLogs |
        Where-Object { $_.LogName -match $RelevantNamePattern } |
        Sort-Object LogName |
        ForEach-Object { Convert-LogMetadata $_ }
)

$enabledRelevantEvents = [ordered]@{}
foreach ($log in @($allLogs | Where-Object {
    $_.IsEnabled -and $_.RecordCount -gt 0 -and $_.LogName -match $RelevantNamePattern
})) {
    $events = @(
        Get-WinEvent -FilterHashtable @{ LogName = $log.LogName; StartTime = $Since } -ErrorAction SilentlyContinue |
            Select-Object -First 1000 |
            ForEach-Object { Convert-EventRecord $_ }
    )
    if ($events.Count -gt 0) { $enabledRelevantEvents[[string]$log.LogName] = $events }
}

$providers = @(
    Get-WinEvent -ListProvider * -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $RelevantNamePattern } |
        Sort-Object Name |
        ForEach-Object { Convert-ProviderMetadata $_ }
)

$systemEvents = @(
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $Since } -ErrorAction SilentlyContinue |
        Select-Object -First 4000 |
        ForEach-Object { Convert-EventRecord $_ }
)

$pnpClasses = @('System', 'USB', 'HIDClass', 'Keyboard', 'Mouse', 'Bluetooth', 'Net', 'MEDIA')
$pnpDevices = @(
    foreach ($class in $pnpClasses) {
        Get-PnpDevice -Class $class -PresentOnly -ErrorAction SilentlyContinue |
            Sort-Object InstanceId |
            ForEach-Object {
                [ordered]@{
                    class = [string]$_.Class
                    friendly_name = [string]$_.FriendlyName
                    instance_id = [string]$_.InstanceId
                    status = [string]$_.Status
                    problem = [int]$_.Problem
                }
            }
    }
)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    since_time = $Since.ToString('o')
    computer_system = Get-CimInstance Win32_ComputerSystem |
        Select-Object Manufacturer, Model, SystemFamily, SystemSKUNumber
    baseboard = Get-CimInstance Win32_BaseBoard |
        Select-Object Manufacturer, Product, Version, SerialNumber
    bios = Get-CimInstance Win32_BIOS |
        Select-Object Manufacturer, SMBIOSBIOSVersion, ReleaseDate, Version
    relevant_logs = $relevantLogs
    relevant_providers = $providers
    enabled_relevant_events = $enabledRelevantEvents
    complete_system_events = $systemEvents
    present_pnp_devices = $pnpDevices
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 16 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ([ordered]@{
    hostname = $result.hostname
    collection_time_utc = $result.collection_time_utc
    relevant_log_count = $relevantLogs.Count
    relevant_provider_count = $providers.Count
    enabled_relevant_log_count = $enabledRelevantEvents.Count
    complete_system_event_count = $systemEvents.Count
    present_pnp_device_count = $pnpDevices.Count
    output_path = $OutputPath
} | ConvertTo-Json -Compress)
