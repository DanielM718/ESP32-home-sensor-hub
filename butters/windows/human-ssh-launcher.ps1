$ErrorActionPreference = 'Stop'

$bash = 'C:\Program Files\Git\bin\bash.exe'
if (-not (Test-Path $bash)) {
    Write-Error 'Git Bash is unavailable.'
    exit 127
}

$original = [string]$env:SSH_ORIGINAL_COMMAND
if ([string]::IsNullOrWhiteSpace($original)) {
    & $bash '--login' '-i'
    exit $LASTEXITCODE
}

if ($original -in @('sftp', 'internal-sftp')) {
    & 'C:\Windows\System32\OpenSSH\sftp-server.exe'
    exit $LASTEXITCODE
}

# This launcher is bound only to the administrator's human public key. The
# command remains intentionally unrestricted for that human control surface.
& $bash '--login' '-c' $original
exit $LASTEXITCODE
