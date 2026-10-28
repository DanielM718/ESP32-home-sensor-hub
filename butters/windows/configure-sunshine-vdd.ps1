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
    desired_mode_advertised = [bool](@($availableModes | Where-Object {
        $_.width -eq $targetWidth -and
        $_.height -eq $targetHeight -and
        $_.refresh -eq $targetRefresh
    }).Count -gt 0)
    desired_width = $targetWidth
    desired_height = $targetHeight
    desired_refresh = $targetRefresh
    available_modes = @($availableModes | Sort-Object width,height,refresh -Unique)
    displays = @($validatedInventory.displays)
}
if ($AuditOnly) {
    $auditResult = [pscustomobject]$audit
    $auditResult | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding UTF8
    $auditResult | ConvertTo-Json -Depth 6 -Compress
    exit 0
}
if (-not $beforeMode) { throw "Could not read the current mode for $displayName" }
if (
    $beforeMode.dmPelsWidth -ne $targetWidth -or
    $beforeMode.dmPelsHeight -ne $targetHeight -or
    $beforeMode.dmDisplayFrequency -ne $targetRefresh
) {
    throw "The VDD must already be ${targetWidth}x${targetHeight} at ${targetRefresh} Hz; this helper will not mutate a volatile DISPLAYn alias"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required'
}

New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
Copy-Item -LiteralPath $sunshineConfig -Destination (Join-Path $backupRoot 'sunshine.conf.before')
$audit | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupRoot 'display-mode-before.json') -Encoding UTF8

# Revalidate immediately before writing configuration. The source alias is an
# observation only: no topology API below consumes it, so an alias reassignment
# can never redirect a mutation to a physical monitor.
$currentVirtual = Get-ButtersVirtualDisplay
if ([string]$currentVirtual.source_name -cne $displayName) {
    throw 'The virtual display enumeration changed during validation'
}
$afterMode = Get-CurrentMode
if ($afterMode.dmPelsWidth -ne $targetWidth -or $afterMode.dmPelsHeight -ne $targetHeight -or $afterMode.dmDisplayFrequency -ne $targetRefresh) {
    throw "The VDD mode did not become ${targetWidth}x${targetHeight} at ${targetRefresh} Hz"
}

$values = [ordered]@{
    output_name = $displayDeviceId
    # The installed Sunshine build persists the initial topology even for
    # verify_only. disabled is the only mode that does not create a snapshot
    # which a later disconnect/app-close/restart path can replay.
    dd_configuration_option = 'disabled'
    dd_resolution_option = 'disabled'
    dd_manual_resolution = '2560x1440'
    dd_refresh_rate_option = 'disabled'
    dd_manual_refresh_rate = '60'
    dd_hdr_option = 'disabled'
    # This changes when reversion is attempted, not whether persisted state
    # exists. It is retained as defense in depth; disabled above is the control
    # that prevents new display-device persistence.
    dd_config_revert_on_disconnect = 'false'
}
$lines = [Collections.Generic.List[string]]::new()
foreach ($line in @(Get-Content -LiteralPath $sunshineConfig)) { $lines.Add([string]$line) }
foreach ($entry in $values.GetEnumerator()) {
    # Not $matches: a successful -match replaces it with the automatic $Matches
    # hashtable, and appending an index to that throws for any existing key.
    $keyIndexes = [Collections.Generic.List[int]]::new()
    for ($index = 0; $index -lt $lines.Count; $index += 1) {
        if ($lines[$index] -match ('^\s*' + [regex]::Escape($entry.Key) + '\s*=')) { $keyIndexes.Add($index) }
    }
    if ($keyIndexes.Count -eq 0) {
        $lines.Add("$($entry.Key) = $($entry.Value)")
        continue
    }
    $lines[$keyIndexes[0]] = "$($entry.Key) = $($entry.Value)"
    for ($keyIndex = $keyIndexes.Count - 1; $keyIndex -ge 1; $keyIndex -= 1) {
        $lines.RemoveAt($keyIndexes[$keyIndex])
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
