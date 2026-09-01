param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Before', 'After')]
    [string]$Phase
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$NicDescription = 'Realtek PCIe 5GbE Family Controller'
$ExpectedMac = '34-5A-60-D7-4C-2C'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$MarkerPath = Join-Path $OutputRoot 'cycle-marker.json'
$LatestEvidencePath = Join-Path $OutputRoot 'cycle-evidence-latest.json'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

function Invoke-PowerCfg {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @(& powercfg.exe @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    [ordered]@{ arguments = @($Arguments); exit_code = $exitCode; output = $lines }
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

function Get-FixedAdapterState {
    $adapter = Get-NetAdapter -IncludeHidden |
        Where-Object { $_.InterfaceDescription -eq $NicDescription } |
        Select-Object -First 1
    if (-not $adapter) { throw "Fixed Realtek adapter was not found: $NicDescription" }

    $power = $null
    $powerError = $null
    try {
        $power = Get-NetAdapterPowerManagement -InterfaceDescription $NicDescription |
            Select-Object Name, InterfaceDescription, AllowComputerToTurnOffDevice,
                ArpOffload, D0PacketCoalescing, DeviceSleepOnDisconnect, NSOffload,
                RsnRekeyOffload, SelectiveSuspend, WakeOnMagicPacket, WakeOnPattern
    }
    catch { $powerError = [string]$_.Exception.Message }

    $pnp = Get-PnpDevice -PresentOnly |
        Where-Object { $_.FriendlyName -eq $NicDescription } |
        Select-Object -First 1

    [ordered]@{
        name = [string]$adapter.Name
        interface_description = [string]$adapter.InterfaceDescription
        status = [string]$adapter.Status
        mac_address = [string]$adapter.MacAddress
        expected_mac_match = ([string]$adapter.MacAddress -eq $ExpectedMac)
        link_speed = [string]$adapter.LinkSpeed
        driver_file_name = [string]$adapter.DriverFileName
        driver_version = [string]$adapter.DriverVersion
        driver_date = [string]$adapter.DriverDate
        pnp_device_id = [string]$adapter.PnPDeviceID
        pnp_status = [string]$pnp.Status
        pnp_problem = if ($pnp) { [int]$pnp.Problem } else { $null }
        power_management = $power
        power_management_error = $powerError
    }
}

function Get-BootTimeUtc {
    (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
}

function Get-MaxRecordId {
    param([Parameter(Mandatory = $true)][string]$LogName)

    $event = Get-WinEvent -LogName $LogName -MaxEvents 1 -ErrorAction SilentlyContinue
    if ($event) { return [int64]$event.RecordId }
    return [int64]0
}

function Write-CompactResult {
    param([Parameter(Mandatory = $true)]$Value)

    Write-Output ($Value | ConvertTo-Json -Depth 14 -Compress)
}

if ($Phase -eq 'Before') {
    $marker = [ordered]@{
        schema_version = 1
        phase = 'before'
        hostname = [string]$env:COMPUTERNAME
        marker_time = (Get-Date).ToString('o')
        marker_time_utc = (Get-Date).ToUniversalTime().ToString('o')
        boot_time_utc = Get-BootTimeUtc
        system_record_id = Get-MaxRecordId 'System'
        task_scheduler_record_id = Get-MaxRecordId 'Microsoft-Windows-TaskScheduler/Operational'
        windows_update_record_id = Get-MaxRecordId 'Microsoft-Windows-WindowsUpdateClient/Operational'
        channel_record_ids = [ordered]@{
            usb_xhci = Get-MaxRecordId 'Microsoft-Windows-USB-USBXHCI-Operational'
            pci = Get-MaxRecordId 'Microsoft-Windows-PCI/Operational'
            ndis = Get-MaxRecordId 'Microsoft-Windows-NDIS/Operational'
            pnp_device_management = Get-MaxRecordId 'Microsoft-Windows-Kernel-PnP/Device Management'
            pnp_driver_watchdog = Get-MaxRecordId 'Microsoft-Windows-Kernel-PnP/Driver Watchdog'
            task_maintenance = Get-MaxRecordId 'Microsoft-Windows-TaskScheduler/Maintenance'
        }
        fixed_adapter = Get-FixedAdapterState
        wake_armed = Invoke-PowerCfg @('/devicequery', 'wake_armed')
        wake_timers = Invoke-PowerCfg @('/waketimers')
        power_requests = Invoke-PowerCfg @('/requests')
    }
    $marker | ConvertTo-Json -Depth 14 | Set-Content -Path $MarkerPath -Encoding UTF8
    Write-CompactResult $marker
    exit 0
}

if (-not (Test-Path $MarkerPath)) { throw 'Cycle marker is missing' }
$marker = Get-Content -Path $MarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
$markerTime = [datetime]$marker.marker_time
$markerSystemRecord = [int64]$marker.system_record_id

$providerPattern = '(?i)Kernel-Power|Power-Troubleshooter|Kernel-General|UserModePowerService|Kernel-PnP|rt640x64|NDIS|USB|USBHUB|USBXHCI|BTHUSB|TaskScheduler|WindowsUpdate|Maintenance'
$systemRaw = @(
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $markerTime.AddSeconds(-5) } -ErrorAction SilentlyContinue |
        Where-Object {
            $_.RecordId -gt $markerSystemRecord -and
            ($_.ProviderName -match $providerPattern -or $_.Level -le 2)
        }
)
$systemEvents = @($systemRaw | ForEach-Object { Convert-EventRecord $_ })

$enterEvent = $systemRaw |
    Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 42 } |
    Sort-Object RecordId -Descending |
    Select-Object -First 1
$resumeEvent = $systemRaw |
    Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Kernel-Power' -and $_.Id -eq 107 } |
    Sort-Object RecordId -Descending |
    Select-Object -First 1
$troubleshooterEvent = $systemRaw |
    Where-Object { $_.ProviderName -eq 'Microsoft-Windows-Power-Troubleshooter' -and $_.Id -eq 1 } |
    Sort-Object RecordId -Descending |
    Select-Object -First 1

$enterConverted = if ($enterEvent) { Convert-EventRecord $enterEvent } else { $null }
$resumeConverted = if ($resumeEvent) { Convert-EventRecord $resumeEvent } else { $null }
$troubleshooterConverted = if ($troubleshooterEvent) { Convert-EventRecord $troubleshooterEvent } else { $null }

$resumeInitiationUtc = $null
$clockCorrection = $null
if ($resumeEvent) {
    $clockCorrectionEvent = $systemRaw |
        Where-Object {
            $_.ProviderName -eq 'Microsoft-Windows-Kernel-General' -and
            $_.Id -eq 1 -and
            $_.RecordId -gt $resumeEvent.RecordId
        } |
        Sort-Object RecordId |
        Select-Object -First 1
    if ($clockCorrectionEvent) {
        $clockCorrection = Convert-EventRecord $clockCorrectionEvent
        if ($clockCorrection.event_data.NewTime) {
            $resumeInitiationUtc = [string]$clockCorrection.event_data.NewTime
        }
    }
    if (-not $resumeInitiationUtc) {
        $resumeInitiationUtc = $resumeEvent.TimeCreated.ToUniversalTime().ToString('o')
    }
}

$targetState = if ($enterConverted) { [string]$enterConverted.event_data.TargetState } else { $null }
$wakeFromState = if ($resumeConverted) { [string]$resumeConverted.event_data.WakeFromState } else { $null }
$hiberPagesWritten = if ($troubleshooterConverted) { [string]$troubleshooterConverted.event_data.HiberPagesWritten } else { $null }
$wakeSource = if ($troubleshooterConverted -and -not [string]::IsNullOrWhiteSpace([string]$troubleshooterConverted.event_data.WakeSourceText)) {
    [string]$troubleshooterConverted.event_data.WakeSourceText
} elseif ($troubleshooterConverted) {
    'Unknown'
} else {
    $null
}

$taskLog = Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction SilentlyContinue
$taskEvents = @()
$taskLogState = if (-not $taskLog) { 'not_found' } elseif (-not $taskLog.IsEnabled) { 'disabled' } else { 'enabled' }
if ($taskLog -and $taskLog.IsEnabled) {
    $taskEvents = @(
        Get-WinEvent -FilterHashtable @{ LogName = $taskLog.LogName; StartTime = $markerTime.AddSeconds(-5) } -ErrorAction SilentlyContinue |
            Where-Object { $_.RecordId -gt [int64]$marker.task_scheduler_record_id } |
            Select-Object -First 1000 |
            ForEach-Object { Convert-EventRecord $_ }
    )
}

$updateLog = Get-WinEvent -ListLog 'Microsoft-Windows-WindowsUpdateClient/Operational' -ErrorAction SilentlyContinue
$updateEvents = @()
$updateLogState = if (-not $updateLog) { 'not_found' } elseif (-not $updateLog.IsEnabled) { 'disabled' } else { 'enabled' }
if ($updateLog -and $updateLog.IsEnabled) {
    $updateEvents = @(
        Get-WinEvent -FilterHashtable @{ LogName = $updateLog.LogName; StartTime = $markerTime.AddSeconds(-5) } -ErrorAction SilentlyContinue |
            Where-Object { $_.RecordId -gt [int64]$marker.windows_update_record_id } |
            Select-Object -First 1000 |
            ForEach-Object { Convert-EventRecord $_ }
    )
}

$channelDefinitions = [ordered]@{
    usb_xhci = 'Microsoft-Windows-USB-USBXHCI-Operational'
    pci = 'Microsoft-Windows-PCI/Operational'
    ndis = 'Microsoft-Windows-NDIS/Operational'
    pnp_device_management = 'Microsoft-Windows-Kernel-PnP/Device Management'
    pnp_driver_watchdog = 'Microsoft-Windows-Kernel-PnP/Driver Watchdog'
    task_maintenance = 'Microsoft-Windows-TaskScheduler/Maintenance'
}
$additionalChannels = [ordered]@{}
foreach ($key in $channelDefinitions.Keys) {
    $logName = [string]$channelDefinitions[$key]
    $log = Get-WinEvent -ListLog $logName -ErrorAction SilentlyContinue
    $state = if (-not $log) { 'not_found' } elseif (-not $log.IsEnabled) { 'disabled' } else { 'enabled' }
    $events = @()
    if ($state -eq 'enabled') {
        $recordId = [int64]0
        if ($marker.channel_record_ids) {
            $property = $marker.channel_record_ids.PSObject.Properties[$key]
            if ($property) { $recordId = [int64]$property.Value }
        }
        $events = @(
            Get-WinEvent -FilterHashtable @{ LogName = $logName; StartTime = $markerTime.AddSeconds(-5) } -ErrorAction SilentlyContinue |
                Where-Object { $_.RecordId -gt $recordId } |
                Select-Object -First 1000 |
                ForEach-Object { Convert-EventRecord $_ }
        )
    }
    $additionalChannels[$key] = [ordered]@{
        log_name = $logName
        state = $state
        events = $events
    }
}

$currentBootTime = Get-BootTimeUtc
$realtekErrors = @($systemEvents | Where-Object { $_.provider -eq 'rt640x64' -and ($_.level -in @('Error', 'Critical') -or $_.message -match '(?i)Hardware IO') })
$ndisErrors = @(
    @($systemEvents | Where-Object { $_.provider -match '(?i)NDIS' -and $_.level -in @('Error', 'Critical') }) +
    @($additionalChannels.ndis.events | Where-Object { $_.level -in @('Error', 'Critical') })
)
$usbErrors = @(
    @($systemEvents | Where-Object { $_.provider -match '(?i)USB|BTHUSB' -and $_.level -in @('Error', 'Critical') }) +
    @($additionalChannels.usb_xhci.events | Where-Object { $_.level -in @('Error', 'Critical') })
)
$deviceErrors = @(@($systemEvents | Where-Object {
    $_.provider -match '(?i)Kernel-PnP|nvlddmkm|amdkmdag|Display' -and
    $_.level -in @('Error', 'Critical')
}) + @($additionalChannels.pnp_device_management.events | Where-Object {
    $_.level -in @('Error', 'Critical') -or $_.event_id -eq 1011
}))

$result = [ordered]@{
    schema_version = 1
    phase = 'after'
    hostname = [string]$env:COMPUTERNAME
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    marker = $marker
    current_boot_time_utc = $currentBootTime
    rebooted_instead = ($currentBootTime -ne [string]$marker.boot_time_utc)
    fixed_adapter = Get-FixedAdapterState
    wake_armed = Invoke-PowerCfg @('/devicequery', 'wake_armed')
    last_wake = Invoke-PowerCfg @('/lastwake')
    wake_timers = Invoke-PowerCfg @('/waketimers')
    power_requests = Invoke-PowerCfg @('/requests')
    entered_s3 = ($targetState -eq '4' -and $wakeFromState -eq '4')
    target_state = $targetState
    effective_state = if ($enterConverted) { [string]$enterConverted.event_data.EffectiveState } else { $null }
    wake_from_state = $wakeFromState
    hybrid_sleep_observed = ([int64]($hiberPagesWritten -as [int64]) -gt 0)
    hiber_pages_written = $hiberPagesWritten
    resume_initiation_time_utc = $resumeInitiationUtc
    windows_resume_time_utc = if ($troubleshooterConverted) { [string]$troubleshooterConverted.event_data.WakeTime } else { $null }
    windows_sleep_time_utc = if ($troubleshooterConverted) { [string]$troubleshooterConverted.event_data.SleepTime } else { $null }
    wake_source = $wakeSource
    enter_event = $enterConverted
    resume_event = $resumeConverted
    clock_correction_event = $clockCorrection
    power_troubleshooter_event = $troubleshooterConverted
    system_events = $systemEvents
    realtek_errors = $realtekErrors
    ndis_errors = $ndisErrors
    usb_errors = $usbErrors
    device_errors = $deviceErrors
    additional_channels = $additionalChannels
    task_scheduler_log_state = $taskLogState
    task_scheduler_events = $taskEvents
    windows_update_log_state = $updateLogState
    windows_update_events = $updateEvents
}

$result | ConvertTo-Json -Depth 14 | Set-Content -Path $LatestEvidencePath -Encoding UTF8
Write-CompactResult $result
