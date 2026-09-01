$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

function Get-SelectedPnpProperties {
    param([Parameter(Mandatory = $true)][string]$InstanceId)

    @(
        Get-PnpDeviceProperty -InstanceId $InstanceId -ErrorAction SilentlyContinue |
            Where-Object { $_.KeyName -match '(?i)Parent|BusReported|Location|Manufacturer|FriendlyName|DeviceDesc|HardwareIds|Power|Wake|DriverVersion|DriverDate' } |
            ForEach-Object {
                [ordered]@{
                    key = [string]$_.KeyName
                    value = $_.Data
                    type = [string]$_.Type
                }
            }
    )
}

function Get-RegistryPowerProperties {
    param([Parameter(Mandatory = $true)][string]$InstanceId)

    $path = 'HKLM:\SYSTEM\CurrentControlSet\Enum\' + $InstanceId + '\Device Parameters'
    try {
        $item = Get-ItemProperty -Path $path -ErrorAction Stop
        @(
            $item.PSObject.Properties |
                Where-Object { $_.Name -match '(?i)wake|power|suspend|idle|d3|d0' } |
                ForEach-Object {
                    [ordered]@{ name = [string]$_.Name; value = $_.Value }
                }
        )
    }
    catch { @() }
}

$wakeEnable = [ordered]@{ ok = $false; error = $null; value = @() }
try {
    $wakeEnable.value = @(
        Get-WmiObject -Namespace 'root\wmi' -Class MSPower_DeviceWakeEnable -ErrorAction Stop |
            Sort-Object InstanceName |
            ForEach-Object {
                $wmiInstance = [string]$_.InstanceName
                $pnpInstance = $wmiInstance -replace '_\d+$', ''
                $pnp = Get-PnpDevice -InstanceId $pnpInstance -ErrorAction SilentlyContinue
                [ordered]@{
                    wmi_instance_name = $wmiInstance
                    enabled = [bool]$_.Enable
                    pnp_instance_id = $pnpInstance
                    pnp_found = [bool]$pnp
                    class = [string]$pnp.Class
                    friendly_name = [string]$pnp.FriendlyName
                    status = [string]$pnp.Status
                    problem = if ($pnp) { [int]$pnp.Problem } else { $null }
                    properties = if ($pnp) { Get-SelectedPnpProperties $pnpInstance } else { @() }
                }
            }
    )
    $wakeEnable.ok = $true
}
catch {
    $wakeEnable.error = [string]$_.Exception.Message
}

$keyboardMouseHid = @(
    Get-PnpDevice -PresentOnly |
        Where-Object { $_.Class -in @('Keyboard', 'Mouse', 'HIDClass') } |
        Sort-Object Class, FriendlyName, InstanceId |
        ForEach-Object {
            [ordered]@{
                class = [string]$_.Class
                friendly_name = [string]$_.FriendlyName
                status = [string]$_.Status
                problem = [int]$_.Problem
                instance_id = [string]$_.InstanceId
                properties = Get-SelectedPnpProperties $_.InstanceId
                registry_power_properties = Get-RegistryPowerProperties $_.InstanceId
            }
        }
)

$audioUsb = @(
    Get-PnpDevice -PresentOnly |
        Where-Object { $_.InstanceId -like 'USB\VID_3142&PID_0C33*' } |
        ForEach-Object {
            [ordered]@{
                class = [string]$_.Class
                friendly_name = [string]$_.FriendlyName
                status = [string]$_.Status
                problem = [int]$_.Problem
                instance_id = [string]$_.InstanceId
                properties = Get-SelectedPnpProperties $_.InstanceId
                registry_power_properties = Get-RegistryPowerProperties $_.InstanceId
            }
        }
)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    powercfg_wake_armed = @(& powercfg.exe /devicequery wake_armed 2>&1 | ForEach-Object { [string]$_ })
    wmi_device_wake_enable = $wakeEnable
    keyboard_mouse_hid_devices = $keyboardMouseHid
    active_usb_audio_tree = $audioUsb
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$jsonPath = Join-Path $OutputRoot 'baseline-wake-device-details.json'
$result | ConvertTo-Json -Depth 14 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output $jsonPath
