$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'acpi-xhci-deep-state.json'
$Since = (Get-Date).AddHours(-24)
$ControllerIds = @(
    'PCI\VEN_1022&DEV_15B6&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0341',
    'PCI\VEN_1022&DEV_15B7&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0441',
    'PCI\VEN_1022&DEV_15B8&SUBSYS_7E701462&REV_00\4&5D6807C&0&0043',
    'PCI\VEN_1022&DEV_43FD&SUBSYS_11421B21&REV_01\8&E99A29B&0&006000400011'
)
$ProviderPattern = '(?i)ACPI|Kernel-Power|Power-Troubleshooter|Kernel-General|Kernel-PnP|UserPnp|UserModePowerService|PCI|USB|UCX|XHCI|DriverFrameworks|rt640x64|NDIS'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Invoke-PowerCfg {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @(& powercfg.exe @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previous
    [ordered]@{ arguments = @($Arguments); exit_code = $exitCode; output = $lines }
}

function Get-PropertyValue {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [Parameter(Mandatory = $true)][string]$KeyName
    )
    $property = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName $KeyName -ErrorAction SilentlyContinue
    if ($property) { return $property.Data }
    return $null
}

function Convert-Device {
    param([Parameter(Mandatory = $true)]$Device)
    $instanceId = [string]$Device.InstanceId
    $driver = if ($signedDriversById.ContainsKey($instanceId)) { $signedDriversById[$instanceId] } else { $null }
    [ordered]@{
        class = [string]$Device.Class
        friendly_name = [string]$Device.FriendlyName
        instance_id = $instanceId
        status = [string]$Device.Status
        problem = [int]$Device.Problem
        parent = Get-PropertyValue $instanceId 'DEVPKEY_Device_Parent'
        location_info = Get-PropertyValue $instanceId 'DEVPKEY_Device_LocationInfo'
        location_paths = @(Get-PropertyValue $instanceId 'DEVPKEY_Device_LocationPaths')
        bus_reported_description = Get-PropertyValue $instanceId 'DEVPKEY_Device_BusReportedDeviceDesc'
        service = Get-PropertyValue $instanceId 'DEVPKEY_Device_Service'
        enumerator = Get-PropertyValue $instanceId 'DEVPKEY_Device_EnumeratorName'
        driver_provider = if ($driver) { [string]$driver.DriverProviderName } else { $null }
        driver_version = if ($driver) { [string]$driver.DriverVersion } else { $null }
        driver_date = if ($driver) { [string]$driver.DriverDate } else { $null }
        inf_name = if ($driver) { [string]$driver.InfName } else { $null }
    }
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
    catch { $eventData['_parse_error'] = [string]$_.Exception.Message }
    [ordered]@{
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

$signedDriversById = @{}
foreach ($signedDriver in @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue)) {
    if ($signedDriver.DeviceID) { $signedDriversById[[string]$signedDriver.DeviceID] = $signedDriver }
}
$present = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)
$deviceMap = @{}
$parentMap = @{}
foreach ($device in $present) {
    $converted = Convert-Device $device
    $deviceMap[$converted.instance_id] = $converted
    if ($converted.parent) {
        $parent = [string]$converted.parent
        if (-not $parentMap.ContainsKey($parent)) {
            $parentMap[$parent] = [System.Collections.Generic.List[string]]::new()
        }
        $parentMap[$parent].Add($converted.instance_id)
    }
}

function Get-DeviceTree {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [int]$Depth = 0
    )
    if ($Depth -gt 8) { return [ordered]@{ instance_id = $InstanceId; truncated = $true } }
    $device = if ($deviceMap.ContainsKey($InstanceId)) { $deviceMap[$InstanceId] } else { $null }
    $children = @()
    if ($parentMap.ContainsKey($InstanceId)) {
        $children = @($parentMap[$InstanceId] | Sort-Object | ForEach-Object { Get-DeviceTree $_ ($Depth + 1) })
    }
    [ordered]@{ device = $device; children = $children }
}

function Get-AncestorChain {
    param([Parameter(Mandatory = $true)][string]$InstanceId)
    $ancestors = [System.Collections.Generic.List[object]]::new()
    $currentId = $InstanceId
    for ($depth = 0; $depth -lt 16; $depth++) {
        if (-not $deviceMap.ContainsKey($currentId)) { break }
        $parent = [string]$deviceMap[$currentId].parent
        if ([string]::IsNullOrWhiteSpace($parent) -or -not $deviceMap.ContainsKey($parent)) { break }
        $ancestors.Add($deviceMap[$parent])
        $currentId = $parent
    }
    @($ancestors)
}

$controllers = @(
    foreach ($controllerId in $ControllerIds) {
        [ordered]@{
            instance_id = $controllerId
            present = $deviceMap.ContainsKey($controllerId)
            tree = Get-DeviceTree $controllerId
        }
    }
)

$relevantLogs = @(
    Get-WinEvent -ListLog * -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.LogName -match $ProviderPattern } |
        Sort-Object LogName |
        ForEach-Object {
            [ordered]@{
                log_name = [string]$_.LogName
                is_enabled = [bool]$_.IsEnabled
                record_count = if ($null -ne $_.RecordCount) { [int64]$_.RecordCount } else { $null }
                last_write_time_utc = if ($_.LastWriteTime) { $_.LastWriteTime.ToUniversalTime().ToString('o') } else { $null }
            }
        }
)
$providers = @(
    Get-WinEvent -ListProvider * -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $ProviderPattern } |
        Sort-Object Name |
        ForEach-Object {
            [ordered]@{ name = [string]$_.Name; logs = @($_.LogLinks | ForEach-Object { [string]$_.LogName }) }
        }
)
$systemEvents = @(
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $Since } -ErrorAction SilentlyContinue |
        Where-Object { $_.ProviderName -match $ProviderPattern -or $_.Level -le 2 } |
        Select-Object -First 5000 |
        ForEach-Object { Convert-EventRecord $_ }
)
$mediaAndAudio = @(
    $present |
        Where-Object { $_.Class -in @('MEDIA', 'AudioEndpoint') -or $_.FriendlyName -match '(?i)audio|fifine' } |
        Sort-Object InstanceId |
        ForEach-Object {
            $instanceId = [string]$_.InstanceId
            [ordered]@{
                device = $deviceMap[$instanceId]
                ancestors = @(Get-AncestorChain $instanceId)
            }
        }
)
$unhealthy = @(
    $present |
        Where-Object { $_.Status -ne 'OK' -or [int]$_.Problem -ne 0 } |
        Sort-Object InstanceId |
        ForEach-Object { Convert-Device $_ }
)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    boot_time_utc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
    bios = Get-CimInstance Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, ReleaseDate, Version
    active_scheme = Invoke-PowerCfg @('/getactivescheme')
    usb_selective_suspend = Invoke-PowerCfg @('/query', 'SCHEME_CURRENT', '2a737441-1930-4402-8d77-b2bebba308a3', '48e6b7a6-50f5-4782-a5d4-53bb8f07e226')
    wake_timer_policy = Invoke-PowerCfg @('/query', 'SCHEME_CURRENT', '238c9fa8-0aad-41ed-83f4-97be242c8f20', 'bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d')
    wake_armed = Invoke-PowerCfg @('/devicequery', 'wake_armed')
    wake_timers = Invoke-PowerCfg @('/waketimers')
    last_wake = Invoke-PowerCfg @('/lastwake')
    power_requests = Invoke-PowerCfg @('/requests')
    request_overrides = Invoke-PowerCfg @('/requestsoverride')
    controllers = $controllers
    present_pnp_count = $present.Count
    unhealthy_pnp_devices = $unhealthy
    media_and_audio_devices = $mediaAndAudio
    relevant_logs = $relevantLogs
    relevant_providers = $providers
    system_events_last_24h = $systemEvents
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 20 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ([ordered]@{
    hostname = $result.hostname
    collection_time_utc = $result.collection_time_utc
    controller_count = $controllers.Count
    unhealthy_pnp_count = $unhealthy.Count
    audio_device_count = $mediaAndAudio.Count
    relevant_log_count = $relevantLogs.Count
    relevant_provider_count = $providers.Count
    system_event_count = $systemEvents.Count
    output_path = $OutputPath
} | ConvertTo-Json -Compress)
