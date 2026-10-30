# Windows-side wrapper for kr260-jtag-por.tcl (same directory).
#   powershell -File kr260-jtag-por.ps1 -Mode check
#   powershell -File kr260-jtag-por.ps1 -Mode por -Confirm confirm-por
# The Tcl script refuses (REFUSED_REACHABLE) unless -BoardAddress is still dark
# immediately before it attaches and again immediately before rst -por.
# Refuses while any debugger process runs, so the hw_server it stops afterwards
# is always the one this run started. It stops that hw_server as soon as XSDB
# exits: after a reset the board's runtime deep-idle setting is gone, and no
# debugger may stay attached while it boots. Prints timestamped lines and ends
# with "RESULT <code>"; exit 0 only for CHECK_OK or RST_ISSUED.
param(
    [ValidateSet('check', 'por')][string]$Mode = 'check',
    [ValidatePattern('^[A-Z0-9]{12}A$')][string]$CableSerial = 'XFL11R5N0BBZA',
    [ValidatePattern('^\d{1,3}(\.\d{1,3}){3}$')][string]$BoardAddress = '192.168.1.125',
    [string]$Confirm = '',
    [string]$Xsdb = 'C:\AMDDesignTools\2026.1\Vivado\bin\xsdb.bat',
    [int]$TimeoutSeconds = 90
)
$ErrorActionPreference = 'Stop'
$debuggers = 'hw_server', 'xsdb', 'rdi_xsdb', 'cs_server', 'vivado'

function Say([string]$text) { '{0} {1}' -f [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ'), $text }

$script = Join-Path $PSScriptRoot 'kr260-jtag-por.tcl'
if (-not (Test-Path $script)) { Say 'RESULT REFUSED_MISSING_SCRIPT'; exit 2 }
$busy = @(Get-Process $debuggers -ErrorAction SilentlyContinue)
if ($busy.Count -gt 0) {
    Say ("RESULT REFUSED_DEBUGGER_ACTIVE " + (($busy | % { "$($_.Name):$($_.Id)" }) -join ','))
    exit 3
}
$arguments = @('-no-ini', "`"$script`"", $Mode, $CableSerial, $BoardAddress)
if ($Mode -eq 'por') { $arguments += $Confirm }

$out = Join-Path $env:TEMP "kr260-jtag-por-$PID.out"
Say "xsdb start mode=$Mode cable=$CableSerial"
$xsdbProcess = Start-Process -FilePath $Xsdb -ArgumentList $arguments -NoNewWindow -PassThru `
    -RedirectStandardOutput $out -RedirectStandardError "$out.err"
if (-not $xsdbProcess.WaitForExit($TimeoutSeconds * 1000)) {
    Say "xsdb exceeded ${TimeoutSeconds}s; stopping it"
    Stop-Process -Id $xsdbProcess.Id -Force -ErrorAction SilentlyContinue
}
# Nothing ran before this call, so every debugger process now is this run's.
foreach ($leftover in @(Get-Process $debuggers -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $leftover.Id -Force -ErrorAction SilentlyContinue
    Say "stopped $($leftover.Name) $($leftover.Id)"
}
Say 'xsdb end'

$lines = @(Get-Content $out -ErrorAction SilentlyContinue) + @(Get-Content "$out.err" -ErrorAction SilentlyContinue)
Remove-Item $out, "$out.err" -ErrorAction SilentlyContinue
$lines | Where-Object { $_ -match '^\s+\d+\*?\s' } | ForEach-Object { Say "listing $_" }
$lines | Where-Object { $_ -match '^PROBE ' } | ForEach-Object { Say $_ }
$result = $lines | Where-Object { $_ -match '^RESULT ' } | Select-Object -Last 1
if (-not $result) { $result = 'RESULT NO_RESULT' }
Say $result
if ($result -match '^RESULT (CHECK_OK|RST_ISSUED)\b') { exit 0 }
exit 1
