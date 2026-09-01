$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

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
    catch { $eventData['_parse_error'] = [string]$_.Exception.Message }
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

$service = Get-CimInstance Win32_Service -Filter "Name='defragsvc'"
$process = $null
if ($service -and $service.ProcessId -gt 0) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($service.ProcessId)" |
        Select-Object ProcessId, ParentProcessId, Name, ExecutablePath, CommandLine,
            CreationDate, KernelModeTime, UserModeTime, WorkingSetSize
}

$task = Get-ScheduledTask -TaskPath '\Microsoft\Windows\Defrag\' -TaskName 'ScheduledDefrag'
$taskInfo = Get-ScheduledTaskInfo -TaskPath $task.TaskPath -TaskName $task.TaskName
$taskXml = Export-ScheduledTask -TaskPath $task.TaskPath -TaskName $task.TaskName

$providers = @(
    Get-WinEvent -ListProvider * -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '(?i)defrag|storag|maintenance' } |
        ForEach-Object {
            [ordered]@{
                name = [string]$_.Name
                logs = @($_.LogLinks | ForEach-Object { [string]$_.LogName } | Sort-Object -Unique)
            }
        }
)

$candidateLogs = @(
    $providers.logs |
        Where-Object { $_ } |
        Sort-Object -Unique
)
$eventsByLog = [ordered]@{}
$start = (Get-Date).AddDays(-7)
foreach ($logName in $candidateLogs) {
    $logInfo = Get-WinEvent -ListLog $logName -ErrorAction SilentlyContinue
    if (-not $logInfo -or -not $logInfo.IsEnabled) {
        $eventsByLog[$logName] = [ordered]@{
            enabled = [bool]($logInfo -and $logInfo.IsEnabled)
            error = if ($logInfo) { 'log_disabled' } else { 'log_not_found' }
            events = @()
        }
        continue
    }
    $events = @(
        Get-WinEvent -FilterHashtable @{ LogName = $logName; StartTime = $start } -ErrorAction SilentlyContinue |
            Where-Object { $_.ProviderName -match '(?i)defrag|storag|maintenance' } |
            Select-Object -First 1000 |
            ForEach-Object { Convert-EventRecord $_ }
    )
    $eventsByLog[$logName] = [ordered]@{ enabled = $true; error = $null; events = $events }
}

$storageJobs = @()
try {
    $storageJobs = @(Get-StorageJob | Select-Object Name, JobState, PercentComplete,
        ElapsedTime, ErrorCode, ErrorDescription)
}
catch { $storageJobs = @([ordered]@{ query_error = [string]$_.Exception.Message }) }

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    power_requests = Invoke-PowerCfg @('/requests')
    wake_timers = Invoke-PowerCfg @('/waketimers')
    service = $service | Select-Object Name, DisplayName, State, StartMode, ProcessId,
        PathName, StartName, ExitCode, ServiceSpecificExitCode
    process = $process
    scheduled_task = [ordered]@{
        task_path = [string]$task.TaskPath
        task_name = [string]$task.TaskName
        state = [string]$task.State
        enabled = [bool]$task.Settings.Enabled
        wake_to_run = [bool]$task.Settings.WakeToRun
        start_when_available = [bool]$task.Settings.StartWhenAvailable
        run_only_if_idle = [bool]$task.Settings.RunOnlyIfIdle
        last_run_time = $taskInfo.LastRunTime.ToString('o')
        next_run_time = if ($taskInfo.NextRunTime.Year -gt 1900) { $taskInfo.NextRunTime.ToString('o') } else { $null }
        last_task_result = [int64]$taskInfo.LastTaskResult
        missed_runs = [int]$taskInfo.NumberOfMissedRuns
        triggers = @($task.Triggers | ForEach-Object {
            [ordered]@{
                type = [string]$_.CimClass.CimClassName
                enabled = [bool]$_.Enabled
                start_boundary = [string]$_.StartBoundary
                random_delay = [string]$_.RandomDelay
                repetition_interval = [string]$_.Repetition.Interval
                repetition_duration = [string]$_.Repetition.Duration
            }
        })
        actions = @($task.Actions | ForEach-Object {
            [ordered]@{
                type = [string]$_.CimClass.CimClassName
                execute = [string]$_.Execute
                arguments = [string]$_.Arguments
            }
        })
        xml = [string]$taskXml
    }
    storage_jobs = $storageJobs
    provider_catalog = $providers
    events_by_log = $eventsByLog
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$jsonPath = Join-Path $OutputRoot 'maintenance-state.json'
$result | ConvertTo-Json -Depth 14 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output $jsonPath
