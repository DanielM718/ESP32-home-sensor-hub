[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ParsecStatus', 'ParsecEnsure', 'ParsecRestart', 'Lock', 'Sleep', 'Restart', 'Shutdown', 'SleepNow')]
    [string]$Operation
)

$ErrorActionPreference = 'Stop'
$parsecServiceName = 'Parsec'
$parsecRoot = 'C:\Program Files\Parsec'
$parsecServiceExecutable = Join-Path $parsecRoot 'pservice.exe'
$parsecHostExecutable = Join-Path $parsecRoot 'parsecd.exe'
$lockTask = '\Butters\LockDesktop'
$sleepTask = '\Butters\SleepDesktop'
$parsecDeadlineSeconds = 20

function Get-ParsecState {
    $service = Get-CimInstance Win32_Service -Filter "Name='Parsec'" -ErrorAction SilentlyContinue
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -in @('pservice.exe', 'parsecd.exe') -and
        $_.ExecutablePath -like "$parsecRoot\*"
    })
    $serviceProcessPresent = [bool]($service -and $service.ProcessId -and ($processes.ProcessId -contains $service.ProcessId))
    $hostProcesses = @($processes | Where-Object { $_.Name -eq 'parsecd.exe' })
    $userProcessPresent = $false
    $systemProcessPresent = $false
    foreach ($process in $hostProcesses) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner -ErrorAction SilentlyContinue
        if ($owner -and $owner.User) {
            if ($owner.User -eq 'SYSTEM') { $systemProcessPresent = $true }
            else { $userProcessPresent = $true }
        }
    }
    $installed = [bool](
        (Test-Path $parsecServiceExecutable) -and
        (Test-Path $parsecHostExecutable) -and
        $service
    )
    $serviceRunning = [bool]($service -and $service.State -eq 'Running')
    [ordered]@{
        installed = $installed
        installation_type = if ($installed) { 'machine' } else { 'absent' }
        service_present = [bool]$service
        service_running = $serviceRunning
        service_startup = if ($service) { ([string]$service.StartMode).ToLowerInvariant() } else { 'absent' }
        service_process_present = $serviceProcessPresent
        host_process_present = $hostProcesses.Count -gt 0
        system_host_process_present = $systemProcessPresent
        user_host_process_present = $userProcessPresent
        plausibly_ready = [bool]($installed -and $serviceRunning -and $serviceProcessPresent -and $hostProcesses.Count -gt 0)
    }
}

function Write-JsonResult([hashtable]$Value) {
    [pscustomobject]$Value | ConvertTo-Json -Depth 4 -Compress
}

function Wait-ParsecReady([datetime]$Deadline) {
    do {
        $state = Get-ParsecState
        if ($state.plausibly_ready) { return $state }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)
    return Get-ParsecState
}

switch ($Operation) {
    'ParsecStatus' {
        Write-JsonResult (Get-ParsecState)
    }
    'ParsecEnsure' {
        $started = Get-Date
        $before = Get-ParsecState
        if (-not $before.installed) {
            Write-JsonResult ([ordered]@{
                accepted = $false
                error_code = 'parsec_not_installed'
                elapsed_ms = 0
            } + $before)
            break
        }
        if (-not $before.service_running) {
            Start-Service -Name $parsecServiceName
        }
        $after = Wait-ParsecReady ((Get-Date).AddSeconds($parsecDeadlineSeconds))
        Write-JsonResult ([ordered]@{
            accepted = [bool]$after.plausibly_ready
            error_code = if ($after.plausibly_ready) { $null } else { 'parsec_start_timeout' }
            already_running = [bool]$before.plausibly_ready
            elapsed_ms = [int]((Get-Date) - $started).TotalMilliseconds
        } + $after)
    }
    'ParsecRestart' {
        $started = Get-Date
        $before = Get-ParsecState
        if (-not $before.installed) {
            Write-JsonResult ([ordered]@{
                accepted = $false
                error_code = 'parsec_not_installed'
                elapsed_ms = 0
            } + $before)
            break
        }
        if ($before.service_running) {
            Restart-Service -Name $parsecServiceName -Force
        } else {
            Start-Service -Name $parsecServiceName
        }
        $after = Wait-ParsecReady ((Get-Date).AddSeconds($parsecDeadlineSeconds))
        Write-JsonResult ([ordered]@{
            accepted = [bool]$after.plausibly_ready
            error_code = if ($after.plausibly_ready) { $null } else { 'parsec_restart_timeout' }
            elapsed_ms = [int]((Get-Date) - $started).TotalMilliseconds
        } + $after)
    }
    'Lock' {
        & schtasks.exe /Run /TN $lockTask | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Fixed lock task failed' }
        Write-JsonResult ([ordered]@{ accepted = $true; transition = 'lock'; scheduled = $true })
    }
    'Sleep' {
        & schtasks.exe /Run /TN $sleepTask | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Fixed sleep task failed' }
        Write-JsonResult ([ordered]@{ accepted = $true; transition = 'sleep'; scheduled = $true })
    }
    'Restart' {
        & shutdown.exe /r /t 5 /d p:0:0 /c 'Butters fixed desktop restart' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Fixed restart request failed' }
        Write-JsonResult ([ordered]@{ accepted = $true; transition = 'restart'; scheduled = $true })
    }
    'Shutdown' {
        & shutdown.exe /s /t 5 /d p:0:0 /c 'Butters fixed desktop shutdown' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Fixed shutdown request failed' }
        Write-JsonResult ([ordered]@{ accepted = $true; transition = 'shutdown'; scheduled = $true })
    }
    'SleepNow' {
        Start-Sleep -Seconds 3
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ButtersPower {
    [DllImport("powrprof.dll", SetLastError = true)]
    public static extern bool SetSuspendState(bool hibernate, bool forceCritical, bool disableWakeEvent);
}
'@
        if (-not [ButtersPower]::SetSuspendState($false, $false, $false)) {
            exit 2
        }
    }
}
