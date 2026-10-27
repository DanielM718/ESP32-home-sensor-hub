[CmdletBinding()]
param(
    [switch]$AuditOnly,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if ($AuditOnly -eq $Apply) {
    throw 'Specify exactly one of -AuditOnly or -Apply'
}

$displayDeviceId = '{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}'
$displayTopologyHelper = 'C:\ProgramData\Butters\display-topology.ps1'
$sunshineConfig = 'C:\Program Files\Sunshine\config\sunshine.conf'
$backupRoot = Join-Path 'C:\ProgramData\Butters\backups' ((Get-Date -Format 'yyyyMMddTHHmmss') + '-sunshine-vdd-config')
$resultPath = 'C:\ProgramData\Butters\sunshine-vdd-config-result.json'
$targetWidth = 2560
$targetHeight = 1440
$targetRefresh = 60

if (-not (Test-Path -LiteralPath $displayTopologyHelper -PathType Leaf)) {
    throw 'The fixed display-topology helper is missing'
}
. $displayTopologyHelper

$root = @(Get-PnpDevice -Class Display -ErrorAction Stop | Where-Object {
    @((Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction SilentlyContinue).Data) -contains 'Root\MttVDD'
})
$child = @(Get-PnpDevice -Class Monitor -ErrorAction Stop | Where-Object {
    $_.InstanceId -like 'DISPLAY\MTT1337\*'
})
$active = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction Stop | Where-Object {
    $_.Active -and $_.InstanceName -like 'DISPLAY\MTT1337\*'
})
if ($root.Count -ne 1 -or [string]$root[0].Status -ne 'OK' -or [int]$root[0].Problem -ne 0) {
    throw 'The Root\MttVDD device is not uniquely present and healthy'
}
if ($child.Count -ne 1 -or [string]$child[0].Status -ne 'OK' -or [int]$child[0].Problem -ne 0) {
    throw 'The DISPLAY\MTT1337 child is not uniquely present and healthy'
}
if ($active.Count -ne 1) { throw 'The MTT1337 virtual monitor is not uniquely active' }
if (-not (Test-Path -LiteralPath $sunshineConfig -PathType Leaf)) { throw 'Sunshine config is missing' }
$validatedInventory = Get-ButtersValidatedDisplayInventory
$displayName = [string]$validatedInventory.virtual.source_name

if (-not ('Butters.NativeDisplayMode' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace Butters {
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct POINTL {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DEVMODE {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion;
        public short dmDriverVersion;
        public short dmSize;
        public short dmDriverExtra;
        public int dmFields;
        public POINTL dmPosition;
        public int dmDisplayOrientation;
        public int dmDisplayFixedOutput;
        public short dmColor;
        public short dmDuplex;
        public short dmYResolution;
        public short dmTTOption;
        public short dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel;
        public int dmPelsWidth;
        public int dmPelsHeight;
        public int dmDisplayFlags;
        public int dmDisplayFrequency;
        public int dmICMMethod;
        public int dmICMIntent;
        public int dmMediaType;
        public int dmDitherType;
        public int dmReserved1;
        public int dmReserved2;
        public int dmPanningWidth;
        public int dmPanningHeight;
    }

    public static class NativeDisplayMode {
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplaySettings(
            string deviceName,
            int modeNum,
            ref DEVMODE devMode
        );

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int ChangeDisplaySettingsEx(
            string deviceName,
            ref DEVMODE devMode,
            IntPtr hwnd,
            uint flags,
            IntPtr lParam
        );
    }
}
'@
}

function New-DevMode {
    $mode = New-Object Butters.DEVMODE
    $mode.dmSize = [Runtime.InteropServices.Marshal]::SizeOf([type][Butters.DEVMODE])
    return $mode
}

function Get-CurrentMode {
    $mode = New-DevMode
    if (-not [Butters.NativeDisplayMode]::EnumDisplaySettings($displayName, -1, [ref]$mode)) {
        throw "Could not read the current mode for $displayName"
    }
    return $mode
}

$desiredMode = $null
$availableModes = @()
$modeNumber = 0
do {
    $candidate = New-DevMode
    $found = [Butters.NativeDisplayMode]::EnumDisplaySettings($displayName, $modeNumber, [ref]$candidate)
    if ($found) {
        $availableModes += [pscustomobject]@{
            width = $candidate.dmPelsWidth
            height = $candidate.dmPelsHeight
            refresh = $candidate.dmDisplayFrequency
        }
    }
    if ($found -and $candidate.dmPelsWidth -eq $targetWidth -and $candidate.dmPelsHeight -eq $targetHeight -and $candidate.dmDisplayFrequency -eq $targetRefresh) {
        $desiredMode = $candidate
        break
    }
    $modeNumber += 1
} while ($found)
$beforeMode = $null
try { $beforeMode = Get-CurrentMode } catch {
    if (-not $AuditOnly) { throw }
}
$audit = [ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
    vdd_root_healthy = $true
    vdd_child_healthy = $true
    virtual_monitor_active = $true
    display_name = $displayName
    display_device_id = $displayDeviceId
    current_width = if ($beforeMode) { $beforeMode.dmPelsWidth } else { $null }
    current_height = if ($beforeMode) { $beforeMode.dmPelsHeight } else { $null }
    current_refresh = if ($beforeMode) { $beforeMode.dmDisplayFrequency } else { $null }
    mode_enumeration_available = $availableModes.Count -gt 0
    desired_mode_advertised = [bool]$desiredMode
    desired_width = $targetWidth
    desired_height = $targetHeight
    desired_refresh = $targetRefresh
    available_modes = @($availableModes | Sort-Object width,height,refresh -Unique)
}
if ($AuditOnly) {
    $auditResult = [pscustomobject]$audit
    $auditResult | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $auditResult | ConvertTo-Json -Depth 6 -Compress
    exit 0
}
if (-not $desiredMode) { throw "The VDD does not advertise ${targetWidth}x${targetHeight} at ${targetRefresh} Hz" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required'
}

New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $sunshineConfig -Destination (Join-Path $backupRoot 'sunshine.conf.before')
$audit | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot 'display-mode-before.json') -Encoding UTF8

# Revalidate immediately before the only topology-affecting call. A changed,
# missing, or ambiguous identity aborts without touching any display, even if
# Windows reassigned all DISPLAYn aliases meanwhile.
$currentVirtual = Get-ButtersVirtualDisplay
if ([string]$currentVirtual.source_name -cne $displayName) {
    throw 'The virtual display enumeration changed during validation'
}
$changeResult = [Butters.NativeDisplayMode]::ChangeDisplaySettingsEx(
    [string]$currentVirtual.source_name,
    [ref]$desiredMode,
    [IntPtr]::Zero,
    1,
    [IntPtr]::Zero
)
if ($changeResult -ne 0) { throw "ChangeDisplaySettingsEx failed with result $changeResult" }
Start-Sleep -Milliseconds 500
$afterMode = Get-CurrentMode
if ($afterMode.dmPelsWidth -ne $targetWidth -or $afterMode.dmPelsHeight -ne $targetHeight -or $afterMode.dmDisplayFrequency -ne $targetRefresh) {
    throw "The VDD mode did not become ${targetWidth}x${targetHeight} at ${targetRefresh} Hz"
}

$values = [ordered]@{
    output_name = $displayDeviceId
    # Sunshine may verify the stable target, but must not own a topology
    # snapshot that it can replay after a Moonlight disconnect.
    dd_configuration_option = 'verify_only'
    dd_resolution_option = 'disabled'
    dd_manual_resolution = '2560x1440'
    dd_refresh_rate_option = 'disabled'
    dd_manual_refresh_rate = '60'
    dd_hdr_option = 'disabled'
    # In the installed build this changes *when* reversion is attempted, not
    # whether it exists. verify_only above ensures there is no applied topology
    # snapshot for that attempt to restore.
    dd_config_revert_on_disconnect = 'false'
}
$lines = [Collections.Generic.List[string]]::new()
foreach ($line in @(Get-Content -LiteralPath $sunshineConfig)) { $lines.Add([string]$line) }
foreach ($entry in $values.GetEnumerator()) {
    $matches = @()
    for ($index = 0; $index -lt $lines.Count; $index += 1) {
        if ($lines[$index] -match ('^\s*' + [regex]::Escape($entry.Key) + '\s*=')) { $matches += $index }
    }
    if ($matches.Count -eq 0) {
        $lines.Add("$($entry.Key) = $($entry.Value)")
        continue
    }
    $lines[$matches[0]] = "$($entry.Key) = $($entry.Value)"
    for ($matchIndex = $matches.Count - 1; $matchIndex -ge 1; $matchIndex -= 1) {
        $lines.RemoveAt($matches[$matchIndex])
    }
}
$temporary = $sunshineConfig + '.vdd-config.tmp'
$lines | Set-Content -LiteralPath $temporary -Encoding UTF8
Move-Item -LiteralPath $temporary -Destination $sunshineConfig -Force

$result = [pscustomobject][ordered]@{
    timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
    configured = $true
    backup_directory = $backupRoot
    display_name = $displayName
    display_device_id = $displayDeviceId
    width = $afterMode.dmPelsWidth
    height = $afterMode.dmPelsHeight
    refresh = $afterMode.dmDisplayFrequency
    hdr_expected = 'Disabled (VDD capability and Sunshine management)'
    sunshine_service_restarted = $false
    physical_displays_modified = $false
}
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
$result | ConvertTo-Json -Depth 6 -Compress
