[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Audit', 'Apply')]
    [string]$Operation
)

$ErrorActionPreference = 'Stop'
$taskPath = '\Butters\'
$taskName = 'SunshineVddConfigure'
$scriptPath = 'C:\ProgramData\Butters\configure-sunshine-vdd.ps1'
$resultPath = 'C:\ProgramData\Butters\sunshine-vdd-config-result.json'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required'
}
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
    throw 'The fixed Sunshine VDD configuration script is missing'
}
if (Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "The temporary fixed task $taskPath$taskName already exists"
}

$argument = if ($Operation -eq 'Audit') { '-AuditOnly' } else { '-Apply' }
$powerShellArguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`" $argument"
$action = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument $powerShellArguments
$taskPrincipal = New-ScheduledTaskPrincipal -UserId 'DESKTOP-G4CFVL1\Daniel' -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
$started = (Get-Date).ToUniversalTime()
if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force }

try {
    Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Action $action -Principal $taskPrincipal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 250
        if (Test-Path -LiteralPath $resultPath) {
            $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
            $timestamp = [datetime]::Parse(
                [string]$result.timestamp_utc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            ).ToUniversalTime()
            if ($timestamp -ge $started.AddSeconds(-1)) {
                $result | ConvertTo-Json -Depth 8 -Compress
                exit 0
            }
        }
        $task = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName
        $info = Get-ScheduledTaskInfo -TaskPath $taskPath -TaskName $taskName
        if ([string]$task.State -eq 'Ready' -and $info.LastRunTime.ToUniversalTime() -ge $started.AddSeconds(-1) -and $info.LastTaskResult -ne 267009) {
            throw "The interactive task exited without a result (LastTaskResult=$($info.LastTaskResult))"
        }
    } while ((Get-Date) -lt $deadline)
    throw 'The interactive Sunshine VDD task timed out'
} finally {
    Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
