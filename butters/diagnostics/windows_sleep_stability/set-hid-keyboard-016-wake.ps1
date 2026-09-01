param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Disable', 'Enable')]
    [string]$State
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$Device = 'HID Keyboard Device (016)'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-WakeArmed {
    @(& powercfg.exe /devicequery wake_armed 2>&1 | ForEach-Object { [string]$_ })
}

$before = Get-WakeArmed
$wasArmed = $before -contains $Device

if ($State -eq 'Disable') {
    if (-not $wasArmed) { throw "$Device was not wake-armed before Disable" }
    $command = "powercfg /devicedisablewake `"$Device`""
    $rollback = "powercfg /deviceenablewake `"$Device`""
    $output = @(& powercfg.exe /devicedisablewake $Device 2>&1 | ForEach-Object { [string]$_ })
}
else {
    if ($wasArmed) { throw "$Device was already wake-armed before Enable" }
    $command = "powercfg /deviceenablewake `"$Device`""
    $rollback = "powercfg /devicedisablewake `"$Device`""
    $output = @(& powercfg.exe /deviceenablewake $Device 2>&1 | ForEach-Object { [string]$_ })
}

$exitCode = $LASTEXITCODE
if ($exitCode -ne 0) { throw "Fixed powercfg change failed with exit code $exitCode" }

$after = Get-WakeArmed
$isArmed = $after -contains $Device
$expectedArmed = $State -eq 'Enable'
if ($isArmed -ne $expectedArmed) { throw 'Wake permission did not reach the expected state' }

$result = [ordered]@{
    schema_version = 1
    change_time = (Get-Date).ToString('o')
    change_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    device = $Device
    old_state = if ($wasArmed) { 'wake_armed' } else { 'wake_not_armed' }
    new_state = if ($isArmed) { 'wake_armed' } else { 'wake_not_armed' }
    command = $command
    rollback_command = $rollback
    command_output = $output
    wake_armed_before = $before
    wake_armed_after = $after
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$jsonPath = Join-Path $OutputRoot 'change-hid-keyboard-016-wake.json'
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output ($result | ConvertTo-Json -Depth 6 -Compress)
