param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Disable', 'Enable')]
    [string]$State
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$UsbSubgroup = '2a737441-1930-4402-8d77-b2bebba308a3'
$SelectiveSuspendSetting = '48e6b7a6-50f5-4782-a5d4-53bb8f07e226'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-SelectiveSuspendState {
    $lines = @(& powercfg.exe /query SCHEME_CURRENT $UsbSubgroup $SelectiveSuspendSetting 2>&1 |
        ForEach-Object { [string]$_ })
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the fixed USB selective suspend setting' }
    $acLine = $lines | Where-Object { $_ -match 'Current AC Power Setting Index' } | Select-Object -First 1
    $dcLine = $lines | Where-Object { $_ -match 'Current DC Power Setting Index' } | Select-Object -First 1
    if (-not $acLine -or $acLine -notmatch '0x([0-9a-fA-F]+)') {
        throw 'Unable to parse AC USB selective suspend state'
    }
    $acValue = [Convert]::ToInt32($Matches[1], 16)
    $dcValue = $null
    if ($dcLine -and $dcLine -match '0x([0-9a-fA-F]+)') {
        $dcValue = [Convert]::ToInt32($Matches[1], 16)
    }
    [ordered]@{ ac_value = $acValue; dc_value = $dcValue; raw = $lines }
}

$before = Get-SelectiveSuspendState
$newValue = if ($State -eq 'Disable') { 0 } else { 1 }
$expectedOld = if ($State -eq 'Disable') { 1 } else { 0 }
if ($before.ac_value -ne $expectedOld) {
    throw "AC USB selective suspend state was $($before.ac_value), expected $expectedOld before $State"
}

$setOutput = @(& powercfg.exe /setacvalueindex SCHEME_CURRENT $UsbSubgroup $SelectiveSuspendSetting $newValue 2>&1 |
    ForEach-Object { [string]$_ })
if ($LASTEXITCODE -ne 0) { throw 'Fixed AC USB selective suspend change failed' }
$activateOutput = @(& powercfg.exe /setactive SCHEME_CURRENT 2>&1 | ForEach-Object { [string]$_ })
if ($LASTEXITCODE -ne 0) { throw 'Unable to reactivate the unchanged current power scheme' }

$after = Get-SelectiveSuspendState
if ($after.ac_value -ne $newValue) { throw 'AC USB selective suspend setting did not reach the expected value' }
if ($after.dc_value -ne $before.dc_value) { throw 'DC USB selective suspend setting changed unexpectedly' }

$command = "powercfg /setacvalueindex SCHEME_CURRENT $UsbSubgroup $SelectiveSuspendSetting $newValue"
$rollback = "powercfg /setacvalueindex SCHEME_CURRENT $UsbSubgroup $SelectiveSuspendSetting $expectedOld"
$result = [ordered]@{
    schema_version = 1
    change_time = (Get-Date).ToString('o')
    change_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    setting = 'USB selective suspend (AC)'
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
$jsonPath = Join-Path $OutputRoot 'change-usb-selective-suspend-ac.json'
$result | ConvertTo-Json -Depth 6 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output ($result | ConvertTo-Json -Depth 6 -Compress)
