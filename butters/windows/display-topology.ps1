$ErrorActionPreference = 'Stop'

# These identities were recorded from Sunshine's display-device log and
# Windows' monitor device paths. Display numbers are deliberately absent: they
# are volatile aliases assigned whenever Windows enumerates the topology.
$script:ButtersDisplayIdentity = [ordered]@{
    VirtualSunshineId = '{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}'
    VirtualInstance = 'DISPLAY\MTT1337\1&15ECD195&4&UID256'
    GigabyteInstance = 'DISPLAY\GBT270B\5&19CEA493&0&UID4353'
    OledInstance = 'DISPLAY\GBT3214\5&19CEA493&0&UID4357'
}

if (-not ('Butters.DisplayConfig' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Butters {
    [StructLayout(LayoutKind.Sequential)]
    internal struct LUID { public uint LowPart; public int HighPart; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct RATIONAL { public uint Numerator; public uint Denominator; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PATH_SOURCE_INFO {
        public LUID adapterId; public uint id; public uint modeInfoIdx; public uint statusFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PATH_TARGET_INFO {
        public LUID adapterId;
        public uint id;
        public uint modeInfoIdx;
        public int outputTechnology;
        public int rotation;
        public int scaling;
        public RATIONAL refreshRate;
        public int scanLineOrdering;
        [MarshalAs(UnmanagedType.Bool)] public bool targetAvailable;
        public uint statusFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct PATH_INFO {
        public PATH_SOURCE_INFO sourceInfo;
        public PATH_TARGET_INFO targetInfo;
        public uint flags;
    }

    [StructLayout(LayoutKind.Explicit, Size = 48)]
    internal struct MODE_UNION { }

    [StructLayout(LayoutKind.Sequential)]
    internal struct MODE_INFO {
        public uint infoType; public uint id; public LUID adapterId; public MODE_UNION modeInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct DEVICE_INFO_HEADER {
        public uint type; public uint size; public LUID adapterId; public uint id;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct SOURCE_DEVICE_NAME {
        public DEVICE_INFO_HEADER header;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string viewGdiDeviceName;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct TARGET_DEVICE_NAME {
        public DEVICE_INFO_HEADER header;
        public uint flags;
        public int outputTechnology;
        public ushort edidManufactureId;
        public ushort edidProductCodeId;
        public uint connectorInstance;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string monitorFriendlyDeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string monitorDevicePath;
    }

    public sealed class DisplayTarget {
        public string SourceName { get; internal set; }
        public string FriendlyName { get; internal set; }
        public string MonitorDevicePath { get; internal set; }
        public ushort EdidManufacturerId { get; internal set; }
        public ushort EdidProductCodeId { get; internal set; }
        public uint ConnectorInstance { get; internal set; }
        public bool Active { get; internal set; }
        public bool Available { get; internal set; }
    }

    public static class DisplayConfig {
        const uint QDC_ALL_PATHS = 1;
        const uint DISPLAYCONFIG_PATH_ACTIVE = 1;
        const uint GET_SOURCE_NAME = 1;
        const uint GET_TARGET_NAME = 2;
        const int ERROR_INSUFFICIENT_BUFFER = 122;

        [DllImport("user32.dll")]
        static extern int GetDisplayConfigBufferSizes(uint flags, out uint paths, out uint modes);

        [DllImport("user32.dll")]
        static extern int QueryDisplayConfig(uint flags, ref uint paths, [Out] PATH_INFO[] pathInfo,
            ref uint modes, [Out] MODE_INFO[] modeInfo, IntPtr topologyId);

        [DllImport("user32.dll")]
        static extern int DisplayConfigGetDeviceInfo(ref SOURCE_DEVICE_NAME requestPacket);

        [DllImport("user32.dll")]
        static extern int DisplayConfigGetDeviceInfo(ref TARGET_DEVICE_NAME requestPacket);

        static void Check(int status, string operation) {
            if (status != 0) throw new Win32Exception(status, operation + " failed");
        }

        public static DisplayTarget[] QueryTargets() {
            for (int attempt = 0; attempt < 3; attempt++) {
                uint pathCount, modeCount;
                Check(GetDisplayConfigBufferSizes(QDC_ALL_PATHS, out pathCount, out modeCount),
                    "GetDisplayConfigBufferSizes");
                var paths = new PATH_INFO[pathCount];
                var modes = new MODE_INFO[modeCount];
                int status = QueryDisplayConfig(QDC_ALL_PATHS, ref pathCount, paths,
                    ref modeCount, modes, IntPtr.Zero);
                if (status == ERROR_INSUFFICIENT_BUFFER) continue;
                Check(status, "QueryDisplayConfig");

                var values = new List<DisplayTarget>();
                for (int index = 0; index < pathCount; index++) {
                    PATH_INFO path = paths[index];
                    var source = new SOURCE_DEVICE_NAME();
                    source.header.type = GET_SOURCE_NAME;
                    source.header.size = (uint)Marshal.SizeOf(typeof(SOURCE_DEVICE_NAME));
                    source.header.adapterId = path.sourceInfo.adapterId;
                    source.header.id = path.sourceInfo.id;
                    string sourceName = "";
                    if (DisplayConfigGetDeviceInfo(ref source) == 0)
                        sourceName = source.viewGdiDeviceName ?? "";

                    var target = new TARGET_DEVICE_NAME();
                    target.header.type = GET_TARGET_NAME;
                    target.header.size = (uint)Marshal.SizeOf(typeof(TARGET_DEVICE_NAME));
                    target.header.adapterId = path.targetInfo.adapterId;
                    target.header.id = path.targetInfo.id;
                    if (DisplayConfigGetDeviceInfo(ref target) != 0) continue;
                    values.Add(new DisplayTarget {
                        SourceName = sourceName,
                        FriendlyName = target.monitorFriendlyDeviceName ?? "",
                        MonitorDevicePath = target.monitorDevicePath ?? "",
                        EdidManufacturerId = target.edidManufactureId,
                        EdidProductCodeId = target.edidProductCodeId,
                        ConnectorInstance = target.connectorInstance,
                        Active = (path.flags & DISPLAYCONFIG_PATH_ACTIVE) != 0,
                        Available = path.targetInfo.targetAvailable
                    });
                }
                return values.ToArray();
            }
            throw new Win32Exception(ERROR_INSUFFICIENT_BUFFER, "Display topology changed repeatedly");
        }
    }
}
'@
}

function ConvertTo-ButtersMonitorInstance([string]$DevicePath) {
    if ($DevicePath -match '^\\\\\?\\DISPLAY#(?<model>[^#]+)#(?<instance>[^#]+)#') {
        return "DISPLAY\$($Matches.model)\$($Matches.instance)"
    }
    return ''
}

function Get-ButtersDisplayInventory {
    $raw = @([Butters.DisplayConfig]::QueryTargets() | ForEach-Object {
        [pscustomobject][ordered]@{
            source_name = [string]$_.SourceName
            monitor_device_path = [string]$_.MonitorDevicePath
            instance_path = ConvertTo-ButtersMonitorInstance ([string]$_.MonitorDevicePath)
            friendly_name = [string]$_.FriendlyName
            edid_manufacturer_id = [int]$_.EdidManufacturerId
            edid_product_code_id = [int]$_.EdidProductCodeId
            connector_instance = [int64]$_.ConnectorInstance
            active = [bool]$_.Active
            available = [bool]$_.Available
        }
    })
    # QDC_ALL_PATHS may describe the same target through several possible
    # source routes. Collapse those routes by stable target identity, but fail
    # if Windows claims two of them are active at once.
    return @($raw | Group-Object instance_path | ForEach-Object {
        $active = @($_.Group | Where-Object { $_.active })
        if ($active.Count -gt 1) {
            throw "Display identity $($_.Name) has multiple active source paths"
        }
        $selected = if ($active.Count -eq 1) { $active[0] } else { $_.Group[0] }
        [pscustomobject][ordered]@{
            source_name = [string]$selected.source_name
            monitor_device_path = [string]$selected.monitor_device_path
            instance_path = [string]$selected.instance_path
            friendly_name = [string]$selected.friendly_name
            edid_manufacturer_id = [int]$selected.edid_manufacturer_id
            edid_product_code_id = [int]$selected.edid_product_code_id
            connector_instance = [int64]$selected.connector_instance
            active = $active.Count -eq 1
            available = @($_.Group | Where-Object { $_.available }).Count -gt 0
        }
    })
}

function Get-ButtersVirtualDisplay {
    $matches = @(Get-ButtersDisplayInventory | Where-Object {
        $_.instance_path -ieq $script:ButtersDisplayIdentity.VirtualInstance
    })
    if ($matches.Count -ne 1) {
        throw 'The stable MTT virtual display identity is missing or ambiguous'
    }
    if (-not $matches[0].active -or -not $matches[0].source_name) {
        throw 'The stable MTT virtual display identity is not active'
    }
    return $matches[0]
}

function Get-ButtersValidatedDisplayInventory {
    $inventory = @(Get-ButtersDisplayInventory)
    foreach ($expected in @(
        $script:ButtersDisplayIdentity.VirtualInstance,
        $script:ButtersDisplayIdentity.GigabyteInstance,
        $script:ButtersDisplayIdentity.OledInstance
    )) {
        $matches = @($inventory | Where-Object { $_.instance_path -ieq $expected })
        if ($matches.Count -ne 1) {
            throw "Display identity $expected is missing or ambiguous"
        }
    }
    $virtual = @($inventory | Where-Object {
        $_.instance_path -ieq $script:ButtersDisplayIdentity.VirtualInstance
    })[0]
    if (-not $virtual.active -or -not $virtual.source_name) {
        throw 'The stable MTT virtual display identity is not active'
    }
    return [pscustomobject][ordered]@{
        virtual = $virtual
        displays = $inventory
    }
}
