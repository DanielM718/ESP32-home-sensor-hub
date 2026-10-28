$ErrorActionPreference = 'Stop'

# Fixed Sunshine session observation and the one authenticated close call.
# Dot-sourced by desktop-control.ps1 only for SunshineSessionStatus and
# SunshineSessionEnd. No caller value reaches any URI, header, or path here.
#
# Evidence, from LizardByte Sunshine v2026.516.143833 source:
# - nvhttp serverinfo (both ports) reports proc::proc.running() as
#   <currentgame>; 0 means no application exists, so nothing can be resumed.
# - /resume runs no prep command, so a pre-stream guard cannot protect it;
#   it resumes whenever currentgame is non-zero.
# - The UDP video/control/audio sockets (47998/47999/48000) are bound only
#   while at least one streaming session holds the broadcast context.
# - POST /api/apps/close authenticates with HTTP Basic, then checks CSRF only
#   when an Origin or Referer header is present, then calls
#   proc::proc.terminate(), which ends every stream on that application.
# No API reports the session count, so "active" versus "resumable" comes from
# the bound stream sockets. Only "terminated" ever permits removing MTT.
$script:SunshineServerInfoUri = 'http://127.0.0.1:47989/serverinfo'
$script:SunshineCloseUri = 'https://127.0.0.1:47990/api/apps/close'
$script:SunshineExecutablePath = 'C:\Program Files\Sunshine\sunshine.exe'
$script:SunshineCertificatePath = 'C:\Program Files\Sunshine\config\credentials\cacert.pem'
$script:SunshineCredentialPath = 'C:\ProgramData\Butters\sunshine-api-credential.json'
$script:SunshineStreamPorts = @(47998, 47999, 48000)
# The broker kills the SSH command at 25s. The whole end operation, including
# PowerShell start-up and the Add-Type compile, has to finish well inside it;
# the Pi re-verifies termination itself, so a short verify window is enough.
$script:SunshineSessionEndBudgetSeconds = 15
$script:SunshineRequestTimeoutMilliseconds = 2000
$script:SunshineResponseLimitBytes = 65536

if (-not ('Butters.SunshineTls' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

namespace Butters {
    // Accept exactly the certificate Sunshine serves from its own credentials
    // directory. A per-request callback leaves process-wide TLS policy alone.
    public static class SunshineTls {
        static string pinnedThumbprint;

        public static void Pin(string thumbprint) { pinnedThumbprint = thumbprint; }

        static bool Validate(object sender, X509Certificate certificate, X509Chain chain, SslPolicyErrors errors) {
            if (certificate == null || String.IsNullOrEmpty(pinnedThumbprint)) return false;
            var presented = new X509Certificate2(certificate);
            return String.Equals(presented.Thumbprint, pinnedThumbprint, StringComparison.OrdinalIgnoreCase);
        }

        public static RemoteCertificateValidationCallback Callback {
            get { return Validate; }
        }
    }
}
'@
}

function Read-ButtersBoundedResponse([Net.WebResponse]$Response) {
    $stream = $Response.GetResponseStream()
    try {
        $buffer = New-Object byte[] ($script:SunshineResponseLimitBytes + 1)
        $total = 0
        while ($total -lt $buffer.Length) {
            $read = $stream.Read($buffer, $total, $buffer.Length - $total)
            if ($read -le 0) { break }
            $total += $read
        }
        if ($total -gt $script:SunshineResponseLimitBytes) { throw 'Sunshine response exceeded its limit' }
        return [Text.Encoding]::UTF8.GetString($buffer, 0, $total)
    } finally {
        $stream.Dispose()
    }
}

function Get-ButtersSunshineAppId {
    $request = [Net.HttpWebRequest]::Create($script:SunshineServerInfoUri)
    $request.Method = 'GET'
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = $script:SunshineRequestTimeoutMilliseconds
    $request.ReadWriteTimeout = $script:SunshineRequestTimeoutMilliseconds
    $response = $request.GetResponse()
    try {
        if ([int]$response.StatusCode -ne 200) { throw 'Sunshine serverinfo failed' }
        $content = Read-ButtersBoundedResponse $response
    } finally {
        $response.Close()
    }
    $settings = New-Object Xml.XmlReaderSettings
    $settings.DtdProcessing = [Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $reader = [Xml.XmlReader]::Create((New-Object IO.StringReader $content), $settings)
    try {
        $document = New-Object Xml.XmlDocument
        $document.Load($reader)
    } finally {
        $reader.Dispose()
    }
    $root = $document.DocumentElement
    if (-not $root -or $root.Name -ne 'root' -or $root.GetAttribute('status_code') -ne '200') {
        throw 'Sunshine serverinfo was not successful'
    }
    $node = $root.SelectSingleNode('currentgame')
    $value = if ($node) { $node.InnerText.Trim() } else { '' }
    if ($value -notmatch '\A[0-9]{1,10}\z') { throw 'Sunshine serverinfo has no current application' }
    # Sunshine derives <state> from the same running() value. Requiring the two
    # to agree means a build that ever blanked currentgame for unpaired HTTP
    # clients reads as unknown, never as "no application".
    $stateNode = $root.SelectSingleNode('state')
    $state = if ($stateNode) { $stateNode.InnerText.Trim() } else { '' }
    $expected = if ([int64]$value -gt 0) { 'SUNSHINE_SERVER_BUSY' } else { 'SUNSHINE_SERVER_FREE' }
    if ($state -cne $expected) { throw 'Sunshine serverinfo state is inconsistent' }
    return [int64]$value
}

function Test-ButtersSunshineStreamBound {
    $owners = @(Get-CimInstance Win32_Process -Filter "Name='sunshine.exe'" -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -eq $script:SunshineExecutablePath
    } | ForEach-Object { [int]$_.ProcessId })
    # serverinfo answered, so Sunshine is running. Not finding its process
    # (unreadable path, other install location) is an unknown, not "unbound".
    if ($owners.Count -eq 0) { throw 'The Sunshine process could not be identified' }
    $bound = @(Get-NetUDPEndpoint -ErrorAction Stop | Where-Object {
        $script:SunshineStreamPorts -contains [int]$_.LocalPort -and $owners -contains [int]$_.OwningProcess
    })
    return $bound.Count -gt 0
}

function Get-ButtersSunshineSessionState {
    $appRunning = $null
    $streamActive = $null
    try { $appRunning = [bool]((Get-ButtersSunshineAppId) -ne 0) } catch {}
    try { $streamActive = [bool](Test-ButtersSunshineStreamBound) } catch {}
    if ($null -eq $appRunning -or $null -eq $streamActive) {
        # Never report a partial observation: an unknown state must not look
        # like "no application" to any caller.
        return [ordered]@{
            api_reachable = [bool]($null -ne $appRunning)
            app_running = $null
            stream_active = $null
            session_state = 'unknown'
        }
    }
    $state = if ($appRunning -and $streamActive) { 'active' }
        elseif ($appRunning) { 'resumable' }
        elseif ($streamActive) { 'ending' }
        else { 'terminated' }
    return [ordered]@{
        api_reachable = $true
        app_running = $appRunning
        stream_active = $streamActive
        session_state = $state
    }
}

function Get-ButtersSunshineCredential {
    if (-not (Test-Path -LiteralPath $script:SunshineCredentialPath -PathType Leaf)) { return $null }
    # Only SYSTEM and Administrators may own or hold any access to the
    # credential. An owner has implicit WRITE_DAC, so a file a standard user
    # pre-created would let that user grant themselves read later.
    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $acl = Get-Acl -LiteralPath $script:SunshineCredentialPath
    $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($allowed -notcontains $owner) { return $null }
    if (-not $acl.AreAccessRulesProtected) { return $null }
    foreach ($rule in @($acl.Access)) {
        if ($rule.AccessControlType -ne 'Allow') { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($allowed -notcontains $sid) { return $null }
    }
    $value = Get-Content -LiteralPath $script:SunshineCredentialPath -Raw | ConvertFrom-Json
    $username = [string]$value.username
    $password = [string]$value.password
    if (-not $username -or -not $password -or $username.Contains(':')) { return $null }
    return [pscustomobject]@{ username = $username; password = $password }
}

function Get-ButtersSunshineCertificateThumbprint {
    $pem = Get-Content -LiteralPath $script:SunshineCertificatePath -Raw
    if ($pem -notmatch '(?s)-----BEGIN CERTIFICATE-----(?<body>.+?)-----END CERTIFICATE-----') {
        throw 'Sunshine certificate is unreadable'
    }
    $bytes = [Convert]::FromBase64String(($Matches.body -replace '\s', ''))
    $certificate = New-Object Security.Cryptography.X509Certificates.X509Certificate2 (, $bytes)
    return $certificate.Thumbprint
}

function Invoke-ButtersSunshineClose($Credential) {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    [Butters.SunshineTls]::Pin((Get-ButtersSunshineCertificateThumbprint))
    $request = [Net.HttpWebRequest]::Create($script:SunshineCloseUri)
    $request.Method = 'POST'
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = $script:SunshineRequestTimeoutMilliseconds
    $request.ReadWriteTimeout = $script:SunshineRequestTimeoutMilliseconds
    $request.ServerCertificateValidationCallback = [Butters.SunshineTls]::Callback
    # Preemptive Basic authentication. No Origin or Referer is sent, which is
    # the non-browser path through Sunshine's CSRF check, and no body.
    $token = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(
        $Credential.username + ':' + $Credential.password
    ))
    $request.Headers.Add('Authorization', 'Basic ' + $token)
    $request.ContentLength = 0
    try {
        $response = $request.GetResponse()
    } catch [Net.WebException] {
        $failed = $_.Exception.Response
        if ($failed -and [int]$failed.StatusCode -in @(401, 403)) { return 'sunshine_api_auth_failed' }
        return 'sunshine_api_unavailable'
    }
    try {
        $content = Read-ButtersBoundedResponse $response
    } finally {
        $response.Close()
    }
    try {
        $value = $content | ConvertFrom-Json
    } catch {
        return 'sunshine_api_unavailable'
    }
    if ($value.status -ne $true) { return 'sunshine_api_unavailable' }
    return $null
}

function Invoke-ButtersSunshineSessionEnd {
    $started = Get-Date
    $before = Get-ButtersSunshineSessionState
    $elapsed = { [int]((Get-Date) - $started).TotalMilliseconds }
    if ($before.session_state -eq 'terminated') {
        return [ordered]@{
            accepted = $true; error_code = $null; elapsed_ms = (& $elapsed); already_terminated = $true
        } + $before
    }
    if ($before.session_state -eq 'unknown') {
        return [ordered]@{
            accepted = $false; error_code = 'sunshine_api_unavailable'; elapsed_ms = (& $elapsed); already_terminated = $false
        } + $before
    }
    $credential = Get-ButtersSunshineCredential
    if (-not $credential) {
        return [ordered]@{
            accepted = $false; error_code = 'sunshine_api_unconfigured'; elapsed_ms = (& $elapsed); already_terminated = $false
        } + $before
    }
    $closeError = Invoke-ButtersSunshineClose $credential
    if ($closeError) {
        return [ordered]@{
            accepted = $false; error_code = $closeError; elapsed_ms = (& $elapsed); already_terminated = $false
        } + (Get-ButtersSunshineSessionState)
    }
    # The close response is not proof. Sunshine ends each session about 150ms
    # after the application stops, then releases the stream sockets.
    $deadline = $started.AddSeconds($script:SunshineSessionEndBudgetSeconds)
    do {
        $after = Get-ButtersSunshineSessionState
        if ($after.session_state -eq 'terminated') { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    $terminated = $after.session_state -eq 'terminated'
    return [ordered]@{
        accepted = $terminated
        error_code = if ($terminated) { $null } else { 'sunshine_session_end_timeout' }
        elapsed_ms = (& $elapsed)
        already_terminated = $false
    } + $after
}
