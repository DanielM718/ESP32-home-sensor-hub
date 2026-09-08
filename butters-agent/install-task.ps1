# Run elevated as the intended interactive user after installing files/credentials.
# Does not register a Session-0 service, set autologin, or modify Windows Firewall.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$PythonW,
    [string]$Root = 'C:\ProgramData\Butters\DesktopAgent',
    [string]$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
)
$ErrorActionPreference='Stop'
if(-not (Test-Path -LiteralPath $PythonW -PathType Leaf)) {throw 'pythonw_missing'}
if(-not (Test-Path -LiteralPath "$Root\agent.toml" -PathType Leaf)) {throw 'config_missing'}
if(Get-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent' -ErrorAction SilentlyContinue) {
    throw 'DesktopAgent task already exists; inspect before replacing'
}
# Administrator-owned immutable program/config tree; interactive token has read/execute only.
& icacls.exe $Root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${User}:(OI)(CI)RX" /Q | Out-Null
if($LASTEXITCODE -ne 0) {throw 'acl_failed'}
Get-ChildItem -LiteralPath $Root -Force | ForEach-Object {
    & icacls.exe $_.FullName /reset /T /Q | Out-Null
    if($LASTEXITCODE -ne 0) {throw 'child_acl_failed'}
}
& icacls.exe $Root /setowner '*S-1-5-32-544' /T /Q | Out-Null
if($LASTEXITCODE -ne 0) {throw 'owner_failed'}
$sid=([Security.Principal.NTAccount]$User).Translate([Security.Principal.SecurityIdentifier]).Value
$escape={param($s) [Security.SecurityElement]::Escape($s)}
$exe=& $escape $PythonW
$arguments=& $escape "-m butters_agent --config `"$Root\agent.toml`""
$start=(Get-Date).AddMinutes(1).ToString('s')
$xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <Triggers>
  <LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId><Delay>PT5S</Delay></LogonTrigger>
  <SessionStateChangeTrigger><Enabled>true</Enabled><StateChange>SessionUnlock</StateChange><UserId>$sid</UserId><Delay>PT5S</Delay></SessionStateChangeTrigger>
  <TimeTrigger><Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><StartBoundary>$start</StartBoundary><Enabled>true</Enabled></TimeTrigger>
 </Triggers>
 <Principals><Principal id="User"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
 <Actions Context="User"><Exec><Command>$exe</Command><Arguments>$arguments</Arguments><WorkingDirectory>$Root</WorkingDirectory></Exec></Actions>
</Task>
"@
Register-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent' -Xml $xml | Out-Null
Start-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent'
Write-Output 'DesktopAgent interactive task installed and started'
