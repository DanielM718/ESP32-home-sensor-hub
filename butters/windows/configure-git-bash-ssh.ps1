# Run over an existing administrator SSH connection after inspecting key options.
# Does not change authentication, sshd_config, services, firewall, or user keys.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$bash = 'C:\Program Files\Git\bin\bash.exe'
if (-not (Test-Path -LiteralPath $bash)) { throw 'Verified Git Bash path is unavailable' }
& $bash -c 'uname -s; git --version'
if ($LASTEXITCODE -ne 0) { throw 'Git Bash preflight failed' }
& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
if ($LASTEXITCODE -ne 0) { throw 'Existing sshd_config is invalid' }
$registry = 'HKLM:\SOFTWARE\OpenSSH'
$key = Get-Item $registry
$names = @('DefaultShell', 'DefaultShellCommandOption', 'DefaultShellEscapeArguments')
$before = @($names | ForEach-Object {
    $exists = $key.GetValueNames() -contains $_
    [pscustomobject]@{
        Name = $_
        Exists = $exists
        Value = $key.GetValue($_)
        Kind = $(if ($exists) { [string]$key.GetValueKind($_) } else { $null })
    }
})
$backup = Join-Path 'C:\ProgramData\Butters' ('ssh-foundation-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup | Out-Null
$before | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $backup 'registry-before.json')
Copy-Item 'C:\ProgramData\ssh\sshd_config' (Join-Path $backup 'sshd_config')
if (Test-Path 'C:\ProgramData\ssh\administrators_authorized_keys') {
    Copy-Item 'C:\ProgramData\ssh\administrators_authorized_keys' (Join-Path $backup 'administrators_authorized_keys')
}
@'
$ErrorActionPreference = 'Stop'
$registry = 'HKLM:\SOFTWARE\OpenSSH'
Get-Content (Join-Path $PSScriptRoot 'registry-before.json') -Raw | ConvertFrom-Json | ForEach-Object {
    if ($_.Exists) {
        New-ItemProperty -Path $registry -Name $_.Name -Value $_.Value -PropertyType $_.Kind -Force | Out-Null
    } else {
        Remove-ItemProperty -Path $registry -Name $_.Name -ErrorAction SilentlyContinue
    }
}
Write-Output 'Previous shell registry values restored. Verify a new SSH connection.'
'@ | Set-Content -Encoding UTF8 (Join-Path $backup 'restore-shell.ps1')
New-ItemProperty -Path $registry -Name DefaultShell -Value $bash -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registry -Name DefaultShellCommandOption -Value '-c' -PropertyType String -Force | Out-Null
Write-Output ('Windows SSH backup: ' + $backup)
Get-ItemProperty $registry | Select-Object DefaultShell,DefaultShellCommandOption | ConvertTo-Json -Compress
