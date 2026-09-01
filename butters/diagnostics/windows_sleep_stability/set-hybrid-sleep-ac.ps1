param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Disable', 'Enable')]
    [string]$State
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$SleepSubgroup = '238c9fa8-0aad-41ed-83f4-97be242c8f20'
$HybridSleepSetting = '94ac6d29-73ce-41a6-809f-6363ba21b47e'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-HybridSleepState {
    $lines = @(& powercfg.exe /query SCHEME_CURRENT $SleepSubgroup $HybridSleepSetting 2>&1 |
        ForEach-Object { [string]$_ })
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the fixed hybrid sleep setting' }
    $acLine = $lines | Where-Object { $_ -match 'Current AC Power Setting Index' } | Select-Object -First 1
    $dcLine = $lines | Where-Object { $_ -match 'Current DC Power Setting Index' } | Select-Object -First 1
    if (-not $acLine -or $acLine -notmatch '0x([0-9a-fA-F]+)') { throw 'Unable to parse AC hybrid sleep state' }
    $acValue = [Convert]::ToInt32($Matches[1], 16)
    $dcValue = $null
    if ($dcLine -and $dcLine -match '0x([0-9a-fA-F]+)') {
        $dcValue = [Convert]::ToInt32($Matches[1], 16)
    }
    [ordered]@{ ac_value = $acValue; dc_value = $dcValue; raw = $lines }
}

$before = Get-HybridSleepState
$newValue = if ($State -eq 'Disable') { 0 } else { 1 }
$expectedOld = if ($State -eq 'Disable') { 1 } else { 0 }
if ($before.ac_value -ne $expectedOld) {
    throw "AC hybrid sleep state was $($before.ac_value), expected $expectedOld before $State"
}

$setOutput = @(& powercfg.exe /setacvalueindex SCHEME_CURRENT $SleepSubgroup $HybridSleepSetting $newValue 2>&1 |
    ForEach-Object { [string]$_ })
if ($LASTEXITCODE -ne 0) { throw 'Fixed AC hybrid sleep change failed' }
$activateOutput = @(& powercfg.exe /setactive SCHEME_CURRENT 2>&1 | ForEach-Object { [string]$_ })
if ($LASTEXITCODE -ne 0) { throw 'Unable to reactivate the unchanged current power scheme' }

$after = Get-HybridSleepState
if ($after.ac_value -ne $newValue) { throw 'AC hybrid sleep setting did not reach the expected value' }
if ($after.dc_value -ne $before.dc_value) { throw 'DC hybrid sleep setting changed unexpectedly' }

$command = "powercfg /setacvalueindex SCHEME_CURRENT $SleepSubgroup $HybridSleepSetting $newValue"
$rollback = "powercfg /setacvalueindex SCHEME_CURRENT $SleepSubgroup $HybridSleepSetting $expectedOld"
$result = [ordered]@{
    schema_version = 1
    change_time = (Get-Date).ToString('o')
    change_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    setting = 'Allow hybrid sleep (AC)'
    old_value = $before.ac_value
    new_value = $after.ac_value
    dc_value_unchanged = $after.dc_value
    command = $command
    activation_command = 'powercfg /setactive SCHEME_CURRENT'
    rollback_command = $rollback
    rollback_activation_command = 'powercfg /setactive SCHEME_CURRENT'
    set_output = $setOutput
    activation_output = $activateOutput
    query_before = $before.raw
    query_after = $after.raw
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$jsonPath = Join-Path $OutputRoot 'change-hybrid-sleep-ac.json'
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output ($result | ConvertTo-Json -Depth 6 -Compress)
