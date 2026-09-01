$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$TargetHostname = 'DESKTOP-G4CFVL1'
$NicDescription = 'Realtek PCIe 5GbE Family Controller'
$OutputRoot = 'C:\ProgramData\Butters\sleep-diagnostic'

if ($env:COMPUTERNAME -ne $TargetHostname) {
    throw "Refusing to run on unexpected host: $($env:COMPUTERNAME)"
}

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null

function Invoke-PowerCfg {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $lines = @(& powercfg.exe @Arguments 2>&1 | ForEach-Object { [string]$_ })
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    [ordered]@{
        arguments = @($Arguments)
        exit_code = $exitCode
        output = $lines
    }
}

function Invoke-Captured {
    param([Parameter(Mandatory = $true)][scriptblock]$Script)

    try {
        [ordered]@{ ok = $true; value = @(& $Script); error = $null }
    }
    catch {
        [ordered]@{ ok = $false; value = @(); error = [string]$_.Exception.Message }
    }
}

function Get-OptionalRegistryValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    try { return Get-ItemPropertyValue -Path $Path -Name $Name -ErrorAction Stop }
    catch { return $null }
}

function Convert-TaskTrigger {
    param([Parameter(Mandatory = $true)]$Trigger)

    [ordered]@{
        type = [string]$Trigger.CimClass.CimClassName
        enabled = [bool]$Trigger.Enabled
        start_boundary = [string]$Trigger.StartBoundary
        end_boundary = [string]$Trigger.EndBoundary
        random_delay = [string]$Trigger.RandomDelay
        delay = [string]$Trigger.Delay
        user_id = [string]$Trigger.UserId
        repetition_interval = [string]$Trigger.Repetition.Interval
        repetition_duration = [string]$Trigger.Repetition.Duration
        repetition_stop_at_duration_end = [bool]$Trigger.Repetition.StopAtDurationEnd
    }
}

$allTasks = @(Get-ScheduledTask)
$wakeTasks = @(
    $allTasks |
        Where-Object { $_.Settings.WakeToRun } |
        ForEach-Object {
            $task = $_
            $info = $null
            try { $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath }
            catch { $info = $null }
            [ordered]@{
                task_path = [string]$task.TaskPath
                task_name = [string]$task.TaskName
                state = [string]$task.State
                enabled = [bool]$task.Settings.Enabled
                wake_to_run = [bool]$task.Settings.WakeToRun
                run_only_if_network_available = [bool]$task.Settings.RunOnlyIfNetworkAvailable
                start_when_available = [bool]$task.Settings.StartWhenAvailable
                principal_user_id = [string]$task.Principal.UserId
                principal_logon_type = [string]$task.Principal.LogonType
                last_run_time = if ($info) { $info.LastRunTime.ToString('o') } else { $null }
                next_run_time = if ($info -and $info.NextRunTime.Year -gt 1900) { $info.NextRunTime.ToString('o') } else { $null }
                last_task_result = if ($info) { [int64]$info.LastTaskResult } else { $null }
                triggers = @(
                    $task.Triggers |
                        Where-Object { $null -ne $_ } |
                        ForEach-Object { Convert-TaskTrigger $_ }
                )
                actions = @($task.Actions | ForEach-Object {
                    [ordered]@{
                        execute = [string]$_.Execute
                        arguments = [string]$_.Arguments
                        working_directory = [string]$_.WorkingDirectory
                        class = [string]$_.CimClass.CimClassName
                    }
                })
            }
        }
)

$maintenanceTasks = @(
    $allTasks |
        Where-Object {
            ($_.TaskPath + $_.TaskName) -match '(?i)maintenance|defrag|optimiz|update|orchestrator|usoclient|musnotification|gaming|silentcleanup'
        } |
        ForEach-Object {
            [ordered]@{
                task_path = [string]$_.TaskPath
                task_name = [string]$_.TaskName
                state = [string]$_.State
                enabled = [bool]$_.Settings.Enabled
                wake_to_run = [bool]$_.Settings.WakeToRun
            }
        }
)

$netAdapters = Invoke-Captured {
    Get-NetAdapter -IncludeHidden |
        Select-Object Name, InterfaceDescription, InterfaceGuid, ifIndex, Status, MacAddress,
            LinkSpeed, MediaType, PhysicalMediaType, DriverInformation, DriverFileName,
            DriverVersion, DriverDate, PnPDeviceID
}

$realtekAdapter = Get-NetAdapter -IncludeHidden |
    Where-Object { $_.InterfaceDescription -eq $NicDescription } |
    Select-Object -First 1

if (-not $realtekAdapter) {
    throw "Fixed Realtek adapter was not found: $NicDescription"
}

$nicAdvanced = Invoke-Captured {
    Get-NetAdapterAdvancedProperty -InterfaceDescription $NicDescription -AllProperties |
        Select-Object Name, InterfaceDescription, DisplayName, DisplayValue,
            RegistryKeyword, RegistryValue
}

$nicPower = Invoke-Captured {
    Get-NetAdapterPowerManagement -InterfaceDescription $NicDescription |
        Select-Object Name, InterfaceDescription, AllowComputerToTurnOffDevice,
            ArpOffload, D0PacketCoalescing, DeviceSleepOnDisconnect, NSOffload,
            RsnRekeyOffload, SelectiveSuspend, WakeOnMagicPacket, WakeOnPattern
}

$nicPnP = Get-PnpDevice -PresentOnly |
    Where-Object { $_.FriendlyName -eq $NicDescription } |
    Select-Object Class, FriendlyName, Status, Problem, InstanceId

$nicDrivers = Get-CimInstance Win32_PnPSignedDriver |
    Where-Object { $_.DeviceName -eq $NicDescription } |
    Select-Object DeviceName, DeviceID, DriverProviderName, DriverVersion, DriverDate,
        InfName, IsSigned, Manufacturer, FriendlyName

$interestingDevices = Get-PnpDevice -PresentOnly |
    Where-Object {
        $_.Class -in @('USB', 'HIDClass', 'Keyboard', 'Mouse', 'Bluetooth', 'AudioEndpoint', 'MEDIA', 'Net') -or
        $_.FriendlyName -match '(?i)usb|xhci|host controller|keyboard|mouse|controller|bluetooth|audio|realtek'
    } |
    Select-Object Class, FriendlyName, Status, Problem, InstanceId

$pcieDevices = Get-PnpDevice -PresentOnly |
    Where-Object { $_.InstanceId -like 'PCI\*' } |
    Select-Object Class, FriendlyName, Status, Problem, InstanceId

$updateServices = Get-CimInstance Win32_Service |
    Where-Object { $_.Name -in @('wuauserv', 'UsoSvc', 'DoSvc', 'BITS', 'WaaSMedicSvc') } |
    Select-Object Name, DisplayName, State, StartMode, ProcessId, PathName

$pendingReboot = [ordered]@{
    component_based_servicing = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
    windows_update = Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    pending_file_rename_operations = [bool](
        (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
    )
}

$powerCfg = [ordered]@{
    available_sleep_states = Invoke-PowerCfg @('/a')
    last_wake = Invoke-PowerCfg @('/lastwake')
    wake_timers = Invoke-PowerCfg @('/waketimers')
    requests = Invoke-PowerCfg @('/requests')
    request_overrides = Invoke-PowerCfg @('/requestsoverride')
    active_scheme = Invoke-PowerCfg @('/getactivescheme')
    sleep_settings = Invoke-PowerCfg @('/query', 'SCHEME_CURRENT', 'SUB_SLEEP')
    pcie_settings = Invoke-PowerCfg @('/query', 'SCHEME_CURRENT', 'SUB_PCIEXPRESS')
    usb_settings = Invoke-PowerCfg @('/query', 'SCHEME_CURRENT', '2a737441-1930-4402-8d77-b2bebba308a3')
    wake_armed = Invoke-PowerCfg @('/devicequery', 'wake_armed')
    wake_programmable = Invoke-PowerCfg @('/devicequery', 'wake_programmable')
    wake_from_any = Invoke-PowerCfg @('/devicequery', 'wake_from_any')
    wake_from_s3_supported = Invoke-PowerCfg @('/devicequery', 'wake_from_S3_supported')
}

$energyPath = Join-Path $OutputRoot 'baseline-energy.html'
$sleepDiagnosticsPath = Join-Path $OutputRoot 'baseline-system-power-report.html'
$energy = Invoke-PowerCfg @('/energy', '/duration', '15', '/output', $energyPath)
$sleepDiagnostics = Invoke-PowerCfg @('/systempowerreport', '/output', $sleepDiagnosticsPath)

$result = [ordered]@{
    schema_version = 1
    collection_time = (Get-Date).ToString('o')
    collection_time_utc = (Get-Date).ToUniversalTime().ToString('o')
    hostname = [string]$env:COMPUTERNAME
    user = [string](whoami.exe)
    is_elevated = [bool](([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
    os = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture, LastBootUpTime, LocalDateTime
    computer_system = Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer, Model, SystemType, WakeUpType, AutomaticManagedPagefile
    bios = Get-CimInstance Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, ReleaseDate
    powercfg = $powerCfg
    wake_to_run_tasks = $wakeTasks
    maintenance_related_tasks = $maintenanceTasks
    network_adapters = $netAdapters
    fixed_realtek_adapter = $realtekAdapter | Select-Object Name, InterfaceDescription,
        InterfaceGuid, ifIndex, Status, MacAddress, LinkSpeed, DriverInformation,
        DriverFileName, DriverVersion, DriverDate, PnPDeviceID
    realtek_advanced_properties = $nicAdvanced
    realtek_power_management = $nicPower
    realtek_pnp = @($nicPnP)
    realtek_drivers = @($nicDrivers)
    interesting_devices = @($interestingDevices)
    pcie_devices = @($pcieDevices)
    windows_update_services = @($updateServices)
    pending_reboot = $pendingReboot
    hiberboot_enabled = Get-OptionalRegistryValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' 'HiberbootEnabled'
    hibernate_enabled = Get-OptionalRegistryValue 'HKLM:\SYSTEM\CurrentControlSet\Control\Power' 'HibernateEnabled'
    energy_report = [ordered]@{ command = $energy; path = $energyPath; exists = Test-Path $energyPath }
    system_sleep_diagnostics = [ordered]@{ command = $sleepDiagnostics; path = $sleepDiagnosticsPath; exists = Test-Path $sleepDiagnosticsPath }
}

$jsonPath = Join-Path $OutputRoot 'baseline-inventory.json'
$result | ConvertTo-Json -Depth 12 | Set-Content -Path $jsonPath -Encoding UTF8
Write-Output $jsonPath
