[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'desktop-control.ps1'
$installRoot = 'C:\ProgramData\Butters'
$target = Join-Path $installRoot 'desktop-control.ps1'
$backupRoot = Join-Path $installRoot ('backups\' + (Get-Date -Format 'yyyyMMddTHHmmss') + '-desktop-control')
$taskPath = '\Butters\'

if (-not (Test-Path $source)) { throw 'desktop-control.ps1 is missing beside the installer' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required'
}

New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
if (Test-Path $target) { Copy-Item $target (Join-Path $backupRoot 'desktop-control.ps1.previous') }

$schedule = New-Object -ComObject 'Schedule.Service'
$schedule.Connect()
$rootFolder = $schedule.GetFolder('\')
try { $null = $schedule.GetFolder($taskPath) }
catch { $null = $rootFolder.CreateFolder('Butters') }

foreach ($taskName in @('LockDesktop', 'SleepDesktop')) {
    $existing = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Export-ScheduledTask -TaskPath $taskPath -TaskName $taskName |
            Set-Content -Encoding Unicode (Join-Path $backupRoot ($taskName + '.xml'))
    }
}
$parsec = Get-CimInstance Win32_Service -Filter "Name='Parsec'" -ErrorAction Stop
[pscustomobject]@{
    service = $parsec.Name
    state = $parsec.State
    start_mode = $parsec.StartMode
    path = $parsec.PathName
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $backupRoot 'parsec-service-before.json')

Copy-Item $source $target -Force

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
$lockAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\rundll32.exe' -Argument 'user32.dll,LockWorkStation'
$lockPrincipal = New-ScheduledTaskPrincipal -UserId 'DESKTOP-G4CFVL1\Daniel' -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskPath $taskPath -TaskName 'LockDesktop' -Action $lockAction -Principal $lockPrincipal -Settings $settings -Force | Out-Null

$sleepArgs = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\ProgramData\Butters\desktop-control.ps1 -Operation SleepNow'
$sleepAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument $sleepArgs
$sleepPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskPath $taskPath -TaskName 'SleepDesktop' -Action $sleepAction -Principal $sleepPrincipal -Settings $settings -Force | Out-Null

# Parsec is deliberately on demand. Installing the fixed helper must not make
# the service a boot/login startup mechanism or start it as an installer side
# effect. ParsecEnsure and ParsecRestart start this one fixed service without
# changing its startup type.
Set-Service -Name 'Parsec' -StartupType Manual

[pscustomobject]@{
    installed_script = $target
    backup_directory = $backupRoot
    lock_task = $taskPath + 'LockDesktop'
    sleep_task = $taskPath + 'SleepDesktop'
    parsec_previous_start_mode = $parsec.StartMode
    parsec_final_start_mode = (Get-CimInstance Win32_Service -Filter "Name='Parsec'").StartMode
    script_sha256 = (Get-FileHash -Algorithm SHA256 $target).Hash
} | ConvertTo-Json -Compress
