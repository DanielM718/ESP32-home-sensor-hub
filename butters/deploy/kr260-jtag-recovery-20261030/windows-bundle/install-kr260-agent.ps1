# Desktop Agent update for KR260 JTAG recovery, local gate OFF. Prepared
# 2026-10-30. Run from an elevated PowerShell in the folder holding this bundle.
#   .\install-kr260-agent.ps1 -Stage Check                 read-only
#   .\install-kr260-agent.ps1 -Stage Install -Authorized   needs the owner's approval
#   .\install-kr260-agent.ps1 -Stage Verify                read-only
#   .\install-kr260-agent.ps1 -Stage Rollback -Authorized  restores the backup
# Install changes exactly: C:\ProgramData\Butters\kr260\ (new, protected),
# DesktopAgent\kr260-jtag.toml (new, enabled = false), the agent's
# site-packages butters_agent (wheel from deploy/desktop-agent-kr260-jtag
# f2f216d), and one restart of the DesktopAgent scheduled task. No Windows
# restart, no other service, no Sunshine, VMware, display or Python change. It
# never runs the wrapper, XSDB or hw_server.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Check', 'Install', 'Verify', 'Rollback')][string]$Stage,
    [switch]$Authorized
)
$ErrorActionPreference = 'Stop'
$Bundle = $PSScriptRoot
$AgentRoot = 'C:\ProgramData\Butters\DesktopAgent'
$Python = "$AgentRoot\venv\Scripts\python.exe"
$SitePackages = "$AgentRoot\venv\Lib\site-packages"
$Kr260Dir = 'C:\ProgramData\Butters\kr260'
$Toml = "$AgentRoot\kr260-jtag.toml"
$BackupRoot = 'C:\ProgramData\Butters\backups'
$User = 'DESKTOP-G4CFVL1\Daniel'
$Debuggers = 'hw_server', 'xsdb', 'rdi_xsdb', 'cs_server', 'vivado'
$Expected = @{
    'butters_agent-0.1.0-py3-none-any.whl' = '5ef9de85334cb12aa4efc5c0437ec61864a29af1f7fd02105864b69ec03ecbeb'
    'kr260\kr260-jtag-por.ps1'             = 'ee16ce3344b07b2787f09b403bd68ebcb82dc0a4454fc9ff2c0618ff19844836'
    'kr260\kr260-jtag-por.tcl'             = 'a7709d8fbdf1c65e5f6b6425f75e629838272c315a79fea3b260b96bb84d73da'
    'kr260-jtag.toml'                      = '7f4d8e93c31853719f411c03e0203ef8f2727f7d058d3a413888685e3dc519ce'
}
$DefaultHello = 'desktop.agent.status,desktop.app.launch,desktop.app.list,desktop.app.status,desktop.session.status'
$script:Failures = 0

function Pass([string]$text) { "PASS $text" }
function Fail([string]$text) { "FAIL $text"; $script:Failures++ }
# Parameter names are deliberately unusual: a script block resolves variables
# dynamically, so a Check parameter called $name would shadow a caller's loop
# variable of the same name inside the test.
function Check([string]$CheckLabel, [scriptblock]$CheckTest) {
    try { if (& $CheckTest) { Pass $CheckLabel } else { Fail $CheckLabel } } catch { Fail "$CheckLabel ($($_.Exception.Message))" }
}
function Finish {
    if ($script:Failures) { "RESULT: $script:Failures check(s) failed"; exit 1 }
    'RESULT: all checks passed'
}
function Sha([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() }
function Elevated {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}
function AgentPython([string]$code) {
    # The agent's own interpreter and code, as the running agent loads them.
    $output = & $Python -B -c $code 2>&1
    if ($LASTEXITCODE) { throw "agent python failed: $output" }
    $output
}
function NoDebuggers { @(Get-Process $Debuggers -ErrorAction SilentlyContinue).Count -eq 0 }
function TaskState { (Get-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent').State }
function Protect([string]$path, [switch]$Directory) {
    if ($Directory) {
        & icacls.exe $path /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' "${User}:(OI)(CI)RX" /Q | Out-Null
    } else {
        & icacls.exe $path /reset /Q | Out-Null
    }
    if ($LASTEXITCODE) { throw "icacls failed on $path" }
    & icacls.exe $path /setowner '*S-1-5-32-544' /T /Q | Out-Null
    if ($LASTEXITCODE) { throw "setowner failed on $path" }
}
function RegistryCheck([string]$path) {
    # The agent's own ACL rule: owner SYSTEM/Administrators, no other writer,
    # for the file and its folder.
    AgentPython "from butters_agent.platform.win32 import Platform; Platform().validate_registry(r'$path'); print('ok')"
}
function AgentProcesses {
    # The venv's pythonw.exe is a launcher; the agent itself is its child, the
    # base interpreter. Both carry this command line.
    @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object {
        $_.CommandLine -like '*-m butters_agent --config*DesktopAgent\agent.toml*' })
}
function StopAgent {
    Stop-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent'
    Start-Sleep -Seconds 2
    # Stop-ScheduledTask may leave the launcher's child running; an old agent
    # left behind would compete with the new one for the connection.
    foreach ($process in AgentProcesses) { Stop-Process -Id $process.ProcessId -Force }
    Start-Sleep -Seconds 1
    if ((AgentProcesses).Count) { throw 'an agent process survived the stop' }
}
function RestartAgent {
    StopAgent
    Start-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent'
}
function AgentLog {
    Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'ButtersAgent\agent.jsonl'
}

function Stage-Check {
    Check 'elevated shell' { Elevated }
    foreach ($name in $Expected.Keys) {
        Check "bundle $name sha256" { (Sha (Join-Path $Bundle $name)) -eq $Expected[$name] }
    }
    Check 'agent venv python present' { Test-Path -LiteralPath $Python }
    Check 'DesktopAgent task running' { (TaskState) -eq 'Running' }
    Check 'Sunshine running' { (Get-Service SunshineService).Status -eq 'Running' }
    Check 'no debugger process running' { NoDebuggers }
    Check 'exactly one agent running (launcher + interpreter)' { (AgentProcesses).Count -eq 2 }
    Check 'kr260 folder not yet present' { -not (Test-Path -LiteralPath $Kr260Dir) }
    Check 'kr260-jtag.toml not yet present' { -not (Test-Path -LiteralPath $Toml) }
    Check 'installed agent advertises the 5 default actions' {
        (AgentPython 'import butters_agent.protocol as p; print(*sorted(p.SCHEMAS), sep=chr(44))') -eq $DefaultHello
    }
    Check 'installed agent loads from site-packages' {
        (AgentPython 'import butters_agent; print(butters_agent.__file__)') -like "$SitePackages\butters_agent\*"
    }
    Finish
}

function Stage-Install {
    if (-not $Authorized) { throw 'Install changes the Desktop Agent; rerun with -Authorized only after the owner approves.' }
    $script:Failures = 0
    Stage-Check | Write-Output
    if ($script:Failures) { throw 'Check failed; nothing was changed.' }
    $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')
    $backup = Join-Path $BackupRoot "butters_agent-pre-kr260-$stamp"
    New-Item -ItemType Directory -Path $backup | Out-Null
    Copy-Item -LiteralPath "$SitePackages\butters_agent" -Destination $backup -Recurse
    Get-ChildItem -LiteralPath $SitePackages -Directory -Filter 'butters_agent-*.dist-info' |
        Copy-Item -Destination $backup -Recurse
    Set-Content -LiteralPath "$BackupRoot\last-kr260-agent-backup.txt" -Value $backup
    "backup: $backup"

    # 1. Protected script folder: created empty, locked, then filled.
    New-Item -ItemType Directory -Path $Kr260Dir | Out-Null
    Protect $Kr260Dir -Directory
    Copy-Item -LiteralPath (Join-Path $Bundle 'kr260\kr260-jtag-por.ps1'), (Join-Path $Bundle 'kr260\kr260-jtag-por.tcl') -Destination $Kr260Dir
    & icacls.exe $Kr260Dir /setowner '*S-1-5-32-544' /T /Q | Out-Null
    # 2. Local gate, disabled. It inherits DesktopAgent's protected ACL.
    Copy-Item -LiteralPath (Join-Path $Bundle 'kr260-jtag.toml') -Destination $Toml
    Protect $Toml
    # 3. Agent code from the hash-checked wheel; no network, no dependency change.
    & $Python -m pip install --no-index --no-deps --force-reinstall --disable-pip-version-check (Join-Path $Bundle 'butters_agent-0.1.0-py3-none-any.whl')
    if ($LASTEXITCODE) { throw 'pip install failed; run -Stage Rollback -Authorized.' }
    # 4. Only the agent process restarts.
    RestartAgent
    Start-Sleep -Seconds 10
    Stage-Verify
}

function Stage-Verify {
    $script:Failures = 0
    Check 'wrapper hash' { (Sha "$Kr260Dir\kr260-jtag-por.ps1") -eq $Expected['kr260\kr260-jtag-por.ps1'] }
    Check 'script hash' { (Sha "$Kr260Dir\kr260-jtag-por.tcl") -eq $Expected['kr260\kr260-jtag-por.tcl'] }
    Check 'toml hash' { (Sha $Toml) -eq $Expected['kr260-jtag.toml'] }
    foreach ($path in "$Kr260Dir\kr260-jtag-por.ps1", "$Kr260Dir\kr260-jtag-por.tcl", $Toml) {
        Check "agent ACL rule accepts $path" { (RegistryCheck $path) -eq 'ok' }
    }
    Check 'agent loads the new package from site-packages' {
        (AgentPython 'import butters_agent.kr260 as k; print(k.__file__)') -like "$SitePackages\butters_agent\*"
    }
    Check 'local gate parses and is disabled' {
        (AgentPython "from butters_agent import kr260; c = kr260.load_config(open(r'$Toml', encoding='utf-8-sig').read()); print(c['enabled'])") -eq 'False'
    }
    Check 'default hello is unchanged (5 actions)' {
        (AgentPython 'import butters_agent.protocol as p; print(*sorted(set(p.SCHEMAS) - set(p.OPTIONAL)), sep=chr(44))') -eq $DefaultHello
    }
    Check 'DesktopAgent task running' { (TaskState) -eq 'Running' }
    Check 'exactly one agent (launcher + interpreter)' { (AgentProcesses).Count -eq 2 }
    $log = AgentLog
    Check 'agent logged kr260_jtag_loaded success=false' {
        Get-Content -LiteralPath $log -Tail 20 | Where-Object { $_ -match '"event": "kr260_jtag_loaded"' -and $_ -match '"success": false' }
    }
    Check 'agent reconnected to Butters' {
        Get-Content -LiteralPath $log -Tail 20 | Where-Object { $_ -match '"event": "connected"' }
    }
    Check 'Sunshine running' { (Get-Service SunshineService).Status -eq 'Running' }
    Check 'no debugger process running' { NoDebuggers }
    Finish
}

function Stage-Rollback {
    if (-not $Authorized) { throw 'Rollback changes the Desktop Agent; rerun with -Authorized.' }
    $backup = Get-Content -LiteralPath "$BackupRoot\last-kr260-agent-backup.txt"
    if (-not (Test-Path -LiteralPath "$backup\butters_agent\protocol.py")) { throw "no usable backup at $backup" }
    # The gate goes first: an enabled gate against an older Butters is rejected at hello.
    if (Test-Path -LiteralPath $Toml) { Move-Item -LiteralPath $Toml -Destination "$Toml.disabled-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ'))" }
    StopAgent
    Remove-Item -LiteralPath "$SitePackages\butters_agent" -Recurse -Force
    Get-ChildItem -LiteralPath $SitePackages -Directory -Filter 'butters_agent-*.dist-info' | Remove-Item -Recurse -Force
    Copy-Item -LiteralPath "$backup\butters_agent" -Destination $SitePackages -Recurse
    Get-ChildItem -LiteralPath $backup -Directory -Filter 'butters_agent-*.dist-info' | Copy-Item -Destination $SitePackages -Recurse
    Start-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent'
    Start-Sleep -Seconds 10
    $script:Failures = 0
    Check 'restored agent has no kr260 module' { -not (Test-Path -LiteralPath "$SitePackages\butters_agent\kr260.py") }
    Check 'restored agent advertises the 5 default actions' {
        (AgentPython 'import butters_agent.protocol as p; print(*sorted(p.SCHEMAS), sep=chr(44))') -eq $DefaultHello
    }
    Check 'DesktopAgent task running' { (TaskState) -eq 'Running' }
    "C:\ProgramData\Butters\kr260 is left in place; without kr260-jtag.toml it is inert."
    Finish
}

switch ($Stage) {
    'Check' { Stage-Check }
    'Install' { Stage-Install }
    'Verify' { Stage-Verify }
    'Rollback' { Stage-Rollback }
}
