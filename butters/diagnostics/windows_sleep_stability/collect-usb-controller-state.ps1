$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'
$OutputPath = Join-Path $OutputRoot 'usb-controller-state.json'
$ControllerIds = @(
    'PCI\VEN_1022&DEV_15B6&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0341',
    'PCI\VEN_1022&DEV_15B7&SUBSYS_7E701462&REV_00\4&2B49E1C6&0&0441',
    'PCI\VEN_1022&DEV_15B8&SUBSYS_7E701462&REV_00\4&5D6807C&0&0043',
    'PCI\VEN_1022&DEV_43FD&SUBSYS_11421B21&REV_01\8&E99A29B&0&006000400011'
)

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-PropertyValue {
    param(
        [Parameter(Mandatory = $true)][string]$InstanceId,
        [Parameter(Mandatory = $true)][string]$KeyName
    )

    $property = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName $KeyName -ErrorAction SilentlyContinue
    if ($property) { return $property.Data }
    return $null
}

$allPresent = @(Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue)
$parentMap = @{}
foreach ($device in $allPresent) {
    $parent = Get-PropertyValue -InstanceId $device.InstanceId -KeyName 'DEVPKEY_Device_Parent'
    if ($parent) {
        if (-not $parentMap.ContainsKey([string]$parent)) {
            $parentMap[[string]$parent] = [System.Collections.Generic.List[object]]::new()
        }
        $parentMap[[string]$parent].Add([ordered]@{
            class = [string]$device.Class
            friendly_name = [string]$device.FriendlyName
            instance_id = [string]$device.InstanceId
            status = [string]$device.Status
            problem = [int]$device.Problem
        })
    }
}

$controllers = @(
    foreach ($instanceId in $ControllerIds) {
        $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
        $signedDriver = Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue |
            Where-Object { $_.DeviceID -eq $instanceId } |
            Select-Object -First 1
        [ordered]@{
            instance_id = $instanceId
            found = [bool]$device
            class = if ($device) { [string]$device.Class } else { $null }
            friendly_name = if ($device) { [string]$device.FriendlyName } else { $null }
            status = if ($device) { [string]$device.Status } else { $null }
            problem = if ($device) { [int]$device.Problem } else { $null }
            parent = Get-PropertyValue -InstanceId $instanceId -KeyName 'DEVPKEY_Device_Parent'
            bus_reported_description = Get-PropertyValue -InstanceId $instanceId -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc'
            driver_date = if ($signedDriver) { [string]$signedDriver.DriverDate } else { $null }
            driver_version = if ($signedDriver) { [string]$signedDriver.DriverVersion } else { $null }
            driver_provider = if ($signedDriver) { [string]$signedDriver.DriverProviderName } else { $null }
            inf_name = if ($signedDriver) { [string]$signedDriver.InfName } else { $null }
            children = if ($parentMap.ContainsKey($instanceId)) { @($parentMap[$instanceId]) } else { @() }
        }
    }
)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    boot_time_utc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
    controllers = $controllers
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$result | ConvertTo-Json -Depth 12 | Set-Content -Path $OutputPath -Encoding UTF8
Write-Output ($result | ConvertTo-Json -Depth 12 -Compress)
