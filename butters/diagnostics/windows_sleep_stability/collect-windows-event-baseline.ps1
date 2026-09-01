$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$LookbackDays = 10
$CorrelationSeconds = 300

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

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
        task = [string]$Event.TaskDisplayName
        opcode = [string]$Event.OpcodeDisplayName
        message = [string]$Event.Message
        event_data = $eventData
    }
}

function Test-NearResume {
    param(
        [Parameter(Mandatory = $true)][datetime]$Timestamp,
        [Parameter(Mandatory = $true)][datetime[]]$ResumeTimes
    )

    foreach ($resume in $ResumeTimes) {
        if ([math]::Abs(($Timestamp - $resume).TotalSeconds) -le $CorrelationSeconds) {
            return $true
        }
    }
    return $false
}

$start = (Get-Date).AddDays(-$LookbackDays)
$providerPattern = '(?i)Kernel-Power|Power-Troubleshooter|Kernel-General|UserModePowerService|Kernel-PnP|rt640x64|NDIS|USB|USBHUB|USBXHCI|TaskScheduler|WindowsUpdate|Maintenance'

$providerCatalog = @(
    Get-WinEvent -ListProvider * -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $providerPattern } |
        ForEach-Object {
            [ordered]@{
                name = [string]$_.Name
                logs = @($_.LogLinks | ForEach-Object { [string]$_.LogName } | Sort-Object -Unique)
            }
        }
)

$logCatalog = @(
    Get-WinEvent -ListLog * -ErrorAction SilentlyContinue |
        Where-Object { $_.LogName -match $providerPattern } |
        ForEach-Object {
            [ordered]@{
                log_name = [string]$_.LogName
                enabled = [bool]$_.IsEnabled
                record_count = [int64]$_.RecordCount
                file_size = [int64]$_.FileSize
                maximum_size = [int64]$_.MaximumSizeInBytes
                mode = [string]$_.LogMode
            }
        }
)

$systemEvents = @(
    Get-WinEvent -FilterHashtable @{ LogName = 'System'; StartTime = $start } -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ProviderName -match $providerPattern -or
            $_.Message -match '(?i)Hardware IO|wake source|entered sleep|resumed from sleep'
        } |
        ForEach-Object { Convert-EventRecord $_ }
)

$resumeTimes = @(
    $systemEvents |
        Where-Object { $_.provider -eq 'Microsoft-Windows-Power-Troubleshooter' -and $_.event_id -eq 1 } |
        ForEach-Object { [datetime]$_.time_created }
)

$correlatedLogs = [ordered]@{}
$candidateLogs = @(
    'Microsoft-Windows-TaskScheduler/Operational',
    'Microsoft-Windows-WindowsUpdateClient/Operational',
    'Microsoft-Windows-USB-USBHUB3/Operational',
    'Microsoft-Windows-USB-UCX/Operational',
    'Microsoft-Windows-Kernel-PnP/Configuration',
    'Microsoft-Windows-Kernel-Power/Thermal-Operational'
)

foreach ($logName in $candidateLogs) {
    $logInfo = Get-WinEvent -ListLog $logName -ErrorAction SilentlyContinue
    if (-not $logInfo) {
        $correlatedLogs[$logName] = [ordered]@{ supported = $false; enabled = $false; error = 'log_not_found'; events = @() }
        continue
    }
    if (-not $logInfo.IsEnabled) {
        $correlatedLogs[$logName] = [ordered]@{ supported = $true; enabled = $false; error = 'log_disabled'; events = @() }
        continue
    }
    try {
        $events = @(
            Get-WinEvent -FilterHashtable @{ LogName = $logName; StartTime = $start } -ErrorAction Stop |
                Where-Object {
                    $resumeTimes.Count -gt 0 -and $_.TimeCreated -and (Test-NearResume $_.TimeCreated $resumeTimes)
                } |
                Select-Object -First 2000 |
                ForEach-Object { Convert-EventRecord $_ }
        )
        $correlatedLogs[$logName] = [ordered]@{ supported = $true; enabled = $true; error = $null; events = $events }
    }
    catch {
        $correlatedLogs[$logName] = [ordered]@{ supported = $true; enabled = $true; error = [string]$_.Exception.Message; events = @() }
    }
}

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    lookback_days = $LookbackDays
    correlation_seconds = $CorrelationSeconds
    provider_catalog = $providerCatalog
    log_catalog = $logCatalog
    system_events = $systemEvents
    resume_times = @($resumeTimes | ForEach-Object { $_.ToString('o') })
    correlated_logs = $correlatedLogs
}

$jsonPath = Join-Path $OutputRoot 'baseline-events.json'
$result | ConvertTo-Json -Depth 14 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output $jsonPath
