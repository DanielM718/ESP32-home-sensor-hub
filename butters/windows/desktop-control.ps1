[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ParsecStatus', 'ParsecEnsure', 'ParsecRestart', 'SunshineServiceStatus', 'SunshineStatus', 'SunshineEnsure', 'SunshineProbeInteractive', 'Lock', 'Sleep', 'Restart', 'Shutdown', 'SleepNow')]
    [string]$Operation
)

$ErrorActionPreference = 'Stop'
$parsecServiceName = 'Parsec'
$parsecRoot = 'C:\Program Files\Parsec'
$parsecServiceExecutable = Join-Path $parsecRoot 'pservice.exe'
$parsecHostExecutable = Join-Path $parsecRoot 'parsecd.exe'
$parsecLog = 'C:\ProgramData\Parsec\log.txt'
$sunshineServiceName = 'SunshineService'
$sunshineRoot = 'C:\Program Files\Sunshine'
$sunshineExecutable = Join-Path $sunshineRoot 'sunshine.exe'
$sunshineDxgiInfo = Join-Path $sunshineRoot 'tools\dxgi-info.exe'
$sunshineConfig = Join-Path $sunshineRoot 'config\sunshine.conf'
$sunshineProbeTask = '\Butters\SunshineCaptureProbe'
$sunshineProbeResult = 'C:\ProgramData\Butters\sunshine-capture-probe.json'
$displayTopologyHelper = 'C:\ProgramData\Butters\display-topology.ps1'
$lockTask = '\Butters\LockDesktop'
$sleepTask = '\Butters\SleepDesktop'
$parsecDeadlineSeconds = 20
$sunshineDeadlineSeconds = 20

if (-not (Test-Path -LiteralPath $displayTopologyHelper -PathType Leaf)) {
    throw 'The fixed display-topology helper is missing'
}
. $displayTopologyHelper

function Invoke-SunshineCaptureProbe {
    $started = (Get-Date).ToUniversalTime()
    & schtasks.exe /Run /TN $sunshineProbeTask | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    $deadline = (Get-Date).AddSeconds(8)
    do {
        if (Test-Path -LiteralPath $sunshineProbeResult) {
            try {
                $probe = Get-Content -LiteralPath $sunshineProbeResult -Raw | ConvertFrom-Json
                $captured = [datetime]::Parse(
                    [string]$probe.timestamp_utc,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::RoundtripKind
                ).ToUniversalTime()
                if ($captured -ge $started.AddSeconds(-1)) {
                    return $probe
                }
            } catch {}
        }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    return [pscustomobject]@{ capture_ready = $false; virtual_display_ready = $false }
}

function Test-DxgiOutputAttached([string]$DxgiOutput, [string]$SourceName) {
    # Judge only the block for exactly this source: a bare prefix regex would
    # let \\.\DISPLAY2 match \\.\DISPLAY26, or borrow a later output's
    # AttachedToDesktop line.
    if (-not $SourceName) { return $false }
    foreach ($block in ($DxgiOutput -split '(?m)^(?=[ \t]*Output Name[ \t]*:)')) {
        if ($block -notmatch '(?m)\A[ \t]*Output Name[ \t]*:[ \t]*(?<name>[^\r\n]*?)[ \t]*\r?$') { continue }
        if ($Matches.name -ine $SourceName) { continue }
        return [bool]($block -match '(?m)^[ \t]*AttachedToDesktop[ \t]*:[ \t]*yes[ \t]*\r?$')
    }
    return $false
}

function Write-SunshineCaptureProbe {
    $dxgiOutput = ''
    try { $dxgiOutput = (& $sunshineDxgiInfo 2>$null | Out-String) } catch {}
    $captureReady = [bool]($dxgiOutput -match '(?ms)Output Name\s*:\s*[^\r\n]+.*?AttachedToDesktop\s*:\s*yes')
    $configuredOutput = ''
    if (Test-Path -LiteralPath $sunshineConfig) {
        $line = Get-Content -LiteralPath $sunshineConfig -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*output_name\s*=' } | Select-Object -First 1
        if ($line) { $configuredOutput = (($line -split '=', 2)[1]).Trim() }
    }
    $stableVirtual = $null
    try { $stableVirtual = Get-ButtersVirtualDisplay } catch {}
    # Sunshine's Windows output_name is the stable device_id from its logs,
    # while dxgi-info reports the current volatile \\.\DISPLAYn source name.
    # Bind the two only after QueryDisplayConfig revalidates the MTT instance.
    $virtualDisplayReady = [bool](
        $stableVirtual -and
        $configuredOutput -ieq $script:ButtersDisplayIdentity.VirtualSunshineId -and
        (Test-DxgiOutputAttached $dxgiOutput ([string]$stableVirtual.source_name))
    )
    $result = [pscustomobject][ordered]@{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
        capture_ready = $captureReady
        virtual_display_ready = $virtualDisplayReady
    }
    $temporary = $sunshineProbeResult + '.tmp'
    $result | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $sunshineProbeResult -Force
    $result | ConvertTo-Json -Compress
}

function Test-ParsecAuthenticationError([object[]]$HostProcesses) {
    if (-not (Test-Path $parsecLog) -or $HostProcesses.Count -eq 0) { return $false }
    $starts = @($HostProcesses | ForEach-Object {
        try { (Get-Process -Id $_.ProcessId -ErrorAction Stop).StartTime } catch { $null }
    } | Where-Object { $_ })
    if ($starts.Count -eq 0) { return $false }
    $earliestStart = ($starts | Sort-Object | Select-Object -First 1).AddSeconds(-2)
    foreach ($line in @(Get-Content -Path $parsecLog -Tail 500 -ErrorAction SilentlyContinue)) {
        if ($line -notmatch '^\[[A-Z] (?<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*Hosting stopped due to error in authentication') { continue }
        $stamp = [datetime]::MinValue
        if ([datetime]::TryParseExact($Matches.stamp, 'yyyy-MM-dd HH:mm:ss', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeLocal, [ref]$stamp) -and $stamp -ge $earliestStart) {
            return $true
        }
    }
    return $false
}

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
    $authenticationError = Test-ParsecAuthenticationError $hostProcesses
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
        authentication_error = $authenticationError
        plausibly_ready = [bool]($installed -and $serviceRunning -and $serviceProcessPresent -and $hostProcesses.Count -gt 0 -and -not $authenticationError)
    }
}

function Get-SunshineServiceState {
    $service = Get-CimInstance Win32_Service -Filter "Name='SunshineService'" -ErrorAction SilentlyContinue
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='sunshine.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -eq $sunshineExecutable
    })
    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty LocalPort)
    $installed = [bool]((Test-Path $sunshineExecutable) -and $service)
    $serviceRunning = [bool]($service -and $service.State -eq 'Running')
    $webListener = $listeners -contains 47990
    $streamListeners = 47984, 47989, 48010 | Where-Object { $listeners -notcontains $_ }
    [ordered]@{
        installed = $installed
        service_present = [bool]$service
        service_running = $serviceRunning
        service_startup = if ($service) { ([string]$service.StartMode).ToLowerInvariant() } else { 'absent' }
        host_process_present = $processes.Count -gt 0
        web_listener_present = $webListener
        stream_listeners_present = @($streamListeners).Count -eq 0
        service_ready = [bool]($installed -and $serviceRunning -and $processes.Count -gt 0 -and $webListener -and @($streamListeners).Count -eq 0)
    }
}

function Get-SunshineState {
    $serviceState = Get-SunshineServiceState
    # DXGI enumeration is session-scoped. Run only explicit readiness probes
    # through the InteractiveToken task; overview reads use the cheap state.
    $probe = if (Test-Path $sunshineDxgiInfo) { Invoke-SunshineCaptureProbe } else { $null }
    [ordered]@{
        installed = $serviceState.installed
        service_present = $serviceState.service_present
        service_running = $serviceState.service_running
        service_startup = $serviceState.service_startup
        host_process_present = $serviceState.host_process_present
        web_listener_present = $serviceState.web_listener_present
        stream_listeners_present = $serviceState.stream_listeners_present
        capture_ready = [bool]($probe -and $probe.capture_ready)
        virtual_display_ready = [bool]($probe -and $probe.virtual_display_ready)
        plausibly_ready = [bool]($serviceState.service_ready -and $probe.capture_ready -and $probe.virtual_display_ready)
    }
}

function Write-JsonResult([hashtable]$Value) {
    [pscustomobject]$Value | ConvertTo-Json -Depth 4 -Compress
}

function Wait-ParsecReady([datetime]$Deadline) {
    do {
        $state = Get-ParsecState
        if ($state.plausibly_ready) {
            # The host process appears before Parsec finishes its account check.
            # Re-observe after a short stability window so an expired login is
            # not briefly reported as ready.
            Start-Sleep -Seconds 3
            return Get-ParsecState
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)
    return Get-ParsecState
}

function Wait-SunshineReady([datetime]$Deadline) {
    do {
        $state = Get-SunshineState
        if ($state.plausibly_ready) { return $state }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $Deadline)
    return Get-SunshineState
}

switch ($Operation) {
    'ParsecStatus' {
        Write-JsonResult (Get-ParsecState)
    }
    'SunshineServiceStatus' {
        Write-JsonResult (Get-SunshineServiceState)
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
            error_code = if ($after.plausibly_ready) { $null } elseif ($after.authentication_error) { 'parsec_authentication_required' } else { 'parsec_start_timeout' }
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
    'SunshineStatus' {
        Write-JsonResult (Get-SunshineState)
    }
    'SunshineEnsure' {
        $started = Get-Date
        $before = Get-SunshineState
        if (-not $before.installed) {
            Write-JsonResult ([ordered]@{
                accepted = $false
                error_code = 'sunshine_not_installed'
                elapsed_ms = 0
                already_running = $false
            } + $before)
            break
        }
        if (-not $before.service_running) {
            Start-Service -Name $sunshineServiceName
        }
        $after = Wait-SunshineReady ((Get-Date).AddSeconds($sunshineDeadlineSeconds))
        $errorCode = if ($after.plausibly_ready) { $null } elseif (-not $after.capture_ready) { 'sunshine_capture_unavailable' } else { 'sunshine_start_timeout' }
        Write-JsonResult ([ordered]@{
            accepted = [bool]$after.plausibly_ready
            error_code = $errorCode
            already_running = [bool]$before.plausibly_ready
            elapsed_ms = [int]((Get-Date) - $started).TotalMilliseconds
        } + $after)
    }
    'SunshineProbeInteractive' {
        Write-SunshineCaptureProbe
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
