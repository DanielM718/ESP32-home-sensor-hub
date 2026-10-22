[CmdletBinding()]
param(
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'

$expectedArchiveHash = 'A701F2272E9FCF382849B24F913C6DD07597B3B1116525F2E90182F019609154'
$expectedCatalogHash = '08A0093FC9B2E32B287A6F8A77CA4DE0A31830D29FC33D2B13A918DC859468F6'
$expectedThumbprint = '3CF8CF26D8BA266C3A483AB7D26D4A818E317D76'
$staging = 'C:\ProgramData\Butters\VDD-Staging-25.7.23'
$archive = Join-Path $staging 'Virtual Display Driver_25.7.23_Neutral_portable_en-US.zip'
$catalog = Join-Path $staging 'package\SignedDrivers\x86\VDD\mttvdd.cat'
$inf = Join-Path $staging 'package\SignedDrivers\x86\VDD\MttVDD.inf'
$nefcon = Join-Path $staging 'nefcon\package\x64\nefconc.exe'
$vddConfig = 'C:\VirtualDisplayDriver\vdd_settings.xml'
$sunshineConfigDirectory = 'C:\Program Files\Sunshine\config'
$classGuid = '{4D36E968-E325-11CE-BFC1-08002BE10318}'
$resultPath = 'C:\ProgramData\Butters\vdd-controlled-result.json'

function Get-HardwareIds([object]$Device) {
    try {
        return @((Get-PnpDeviceProperty -InstanceId $Device.InstanceId -KeyName DEVPKEY_Device_HardwareIds -ErrorAction Stop).Data)
    } catch {
        return @()
    }
}

function Get-DevicePropertyValue([object]$Device, [string]$KeyName) {
    try {
        return (Get-PnpDeviceProperty -InstanceId $Device.InstanceId -KeyName $KeyName -ErrorAction Stop).Data
    } catch {
        return $null
    }
}

function Get-DeviceRows([string]$Class) {
    @(Get-PnpDevice -Class $Class -ErrorAction SilentlyContinue | ForEach-Object {
        [pscustomobject][ordered]@{
            friendly_name = $_.FriendlyName
            instance_id = $_.InstanceId
            status = [string]$_.Status
            problem = [int]$_.Problem
            hardware_ids = @(Get-HardwareIds $_)
        }
    })
}

function Get-MttRootDevices {
    @(Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | Where-Object {
        (Get-HardwareIds $_) -contains 'Root\MttVDD'
    })
}

function Get-MttChildMonitors {
    @(Get-PnpDevice -Class Monitor -ErrorAction SilentlyContinue | Where-Object {
        $_.InstanceId -like 'DISPLAY\MTT1337\*' -or
        (Get-HardwareIds $_) -contains 'MONITOR\MTT1337'
    })
}

function Get-ActiveMttMonitors {
    @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | Where-Object {
        $_.Active -and $_.InstanceName -like 'DISPLAY\MTT1337\*'
    } | Select-Object InstanceName,Active,
        @{n='FriendlyName';e={-join ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object {[char]$_})}},
        @{n='Serial';e={-join ($_.SerialNumberID | Where-Object { $_ -ne 0 } | ForEach-Object {[char]$_})}})
}

function Find-MttPublishedInf {
    $text = (& pnputil.exe /enum-drivers /class Display 2>&1 | Out-String)
    foreach ($block in ($text -split '(?:\r?\n){2,}')) {
        if ($block -notmatch '(?im)^Original Name:\s*MttVDD\.inf\s*$') { continue }
        if ($block -match '(?im)^Published Name:\s*(oem\d+\.inf)\s*$') { return $Matches[1] }
    }
    return $null
}

function Get-CurrentState {
    [pscustomobject][ordered]@{
        timestamp_utc = (Get-Date).ToUniversalTime().ToString('o')
        display_devices = @(Get-DeviceRows 'Display')
        monitor_devices = @(Get-DeviceRows 'Monitor')
        active_monitors = @(Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | Select-Object InstanceName,Active,
            @{n='FriendlyName';e={-join ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object {[char]$_})}},
            @{n='Serial';e={-join ($_.SerialNumberID | Where-Object { $_ -ne 0 } | ForEach-Object {[char]$_})}})
        video_controllers = @(Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,Status,DriverVersion,InfFilename,VideoModeDescription,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
        services = @(Get-Service SunshineService,Tailscale -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType)
        desktop_agent_task = @(Get-ScheduledTask -TaskPath '\Butters\' -TaskName 'DesktopAgent' -ErrorAction SilentlyContinue | Select-Object TaskPath,TaskName,State)
        trusted_publisher_present = [bool](Get-ChildItem Cert:\LocalMachine\TrustedPublisher | Where-Object Thumbprint -eq $expectedThumbprint)
        trusted_root_present = [bool](Get-ChildItem Cert:\LocalMachine\Root | Where-Object Thumbprint -eq $expectedThumbprint)
        published_inf = Find-MttPublishedInf
        mtt_root_devices = @(Get-MttRootDevices | Select-Object FriendlyName,InstanceId,Status,Problem)
        mtt_child_monitors = @(Get-MttChildMonitors | Select-Object FriendlyName,InstanceId,Status,Problem)
        active_mtt_monitors = @(Get-ActiveMttMonitors)
    }
}

function Write-Result([object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 10
    $temporary = $resultPath + '.tmp'
    $json | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $resultPath -Force
    $Value | ConvertTo-Json -Depth 10 -Compress
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required'
}
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Expected AMD64 Windows' }

foreach ($required in @($archive, $catalog, $inf, $nefcon, $vddConfig)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Required file is missing: $required" }
}

$archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
$catalogHash = (Get-FileHash -LiteralPath $catalog -Algorithm SHA256).Hash
if ($archiveHash -ne $expectedArchiveHash) { throw 'VDD archive hash mismatch' }
if ($catalogHash -ne $expectedCatalogHash) { throw 'VDD catalog hash mismatch' }
$signature = Get-AuthenticodeSignature -LiteralPath $catalog
if (-not $signature.SignerCertificate -or $signature.SignerCertificate.Thumbprint -ne $expectedThumbprint) {
    throw 'VDD catalog signer mismatch'
}
if ($signature.Status -notin @('Valid', 'NotTrusted')) {
    throw "VDD catalog signature status is $($signature.Status)"
}
if (Get-MttRootDevices) { throw 'An MttVDD root device already exists' }
if (Get-MttChildMonitors) { throw 'An MTT1337 child monitor already exists' }
if (Find-MttPublishedInf) { throw 'An MttVDD driver package is already published' }

$preflight = [pscustomobject][ordered]@{
    preflight_ok = $true
    archive_sha256 = $archiveHash
    catalog_sha256 = $catalogHash
    signature_status = [string]$signature.Status
    signer_subject = $signature.SignerCertificate.Subject
    signer_thumbprint = $signature.SignerCertificate.Thumbprint
    validator_display_count = @(Get-PnpDevice -Class Display -ErrorAction SilentlyContinue).Count
    validator_monitor_count = @(Get-PnpDevice -Class Monitor -ErrorAction SilentlyContinue).Count
    mtt_root_count = @(Get-MttRootDevices).Count
    mtt_child_count = @(Get-MttChildMonitors).Count
}
if ($preflight.validator_display_count -lt 1) { throw 'Corrected validator returned no display adapters during preflight' }
if ($preflight.validator_monitor_count -lt 1) { throw 'Corrected validator returned no monitors during preflight' }
if ($PreflightOnly) {
    Write-Result $preflight
    exit 0
}

$backupRoot = Join-Path 'C:\ProgramData\Butters\backups' ((Get-Date -Format 'yyyyMMddTHHmmss') + '-vdd-controlled-install')
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$publisherWasPresent = [bool](Get-ChildItem Cert:\LocalMachine\TrustedPublisher | Where-Object Thumbprint -eq $expectedThumbprint)
$rootWasPresent = [bool](Get-ChildItem Cert:\LocalMachine\Root | Where-Object Thumbprint -eq $expectedThumbprint)
$mutationStarted = $false
$publishedInf = $null

function Invoke-Rollback {
    try { & $nefcon --remove-device-node --hardware-id 'ROOT\MttVDD' --class-guid $classGuid | Out-Null } catch {}
    $installedInf = Find-MttPublishedInf
    if ($installedInf) {
        try { & pnputil.exe /delete-driver $installedInf /uninstall | Out-Null } catch {}
    }
    if (-not $publisherWasPresent) {
        Get-ChildItem Cert:\LocalMachine\TrustedPublisher | Where-Object Thumbprint -eq $expectedThumbprint | Remove-Item -Force -ErrorAction SilentlyContinue
    }
    $savedVdd = Join-Path $backupRoot 'vdd_settings.xml.before'
    if (Test-Path -LiteralPath $savedVdd) { Copy-Item -LiteralPath $savedVdd -Destination $vddConfig -Force }
}

try {
    Get-CurrentState | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $backupRoot 'state-before.json') -Encoding UTF8
    Copy-Item -LiteralPath $vddConfig -Destination (Join-Path $backupRoot 'vdd_settings.xml.before')
    if (Test-Path -LiteralPath $sunshineConfigDirectory) {
        Copy-Item -LiteralPath $sunshineConfigDirectory -Destination (Join-Path $backupRoot 'sunshine-config-before') -Recurse
    }
    & reg.exe export 'HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\Configuration' (Join-Path $backupRoot 'graphics-configuration-before.reg') /y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not export physical display configuration' }
    & reg.exe export 'HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\Connectivity' (Join-Path $backupRoot 'graphics-connectivity-before.reg') /y | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not export physical display connectivity' }
    & reg.exe export 'HKLM\SYSTEM\CurrentControlSet\Control\GraphicsDrivers\ScaleFactors' (Join-Path $backupRoot 'graphics-scale-before.reg') /y | Out-Null
    # ScaleFactors is absent on some systems, so its export is best-effort.
    $certificatePath = Join-Path $backupRoot 'SignPath-Foundation-3CF8CF26.cer'
    Export-Certificate -Cert $signature.SignerCertificate -FilePath $certificatePath -Type CERT | Out-Null
    $exported = New-Object Security.Cryptography.X509Certificates.X509Certificate2($certificatePath)
    if ($exported.Thumbprint -ne $expectedThumbprint) { throw 'Exported signer certificate mismatch' }

    $rollbackText = @"
Run from elevated PowerShell. Do not import the graphics registry snapshots unless display configuration is damaged.
1. Restore Sunshine config from: $backupRoot\sunshine-config-before
2. Remove Root\MttVDD with: $nefcon --remove-device-node --hardware-id ROOT\MttVDD --class-guid $classGuid
3. Delete only the OEM INF recorded in install-result.json: pnputil /delete-driver oemNN.inf /uninstall
4. If it was absent before this attempt, remove only certificate $expectedThumbprint from LocalMachine\TrustedPublisher.
5. Restore: $backupRoot\vdd_settings.xml.before to $vddConfig
"@
    $rollbackText | Set-Content -LiteralPath (Join-Path $backupRoot 'ROLLBACK.txt') -Encoding UTF8

    [xml]$settings = Get-Content -LiteralPath $vddConfig -Raw
    $settings.vdd_settings.monitors.count = '1'
    $settings.vdd_settings.gpu.friendlyname = 'NVIDIA GeForce RTX 5080'
    foreach ($optionName in @('CustomEdid', 'EdidCeaOverride', 'SDR10bit', 'HDRPlus')) {
        $node = $settings.vdd_settings.options.SelectSingleNode($optionName)
        if ($node) { $node.InnerText = 'false' }
    }
    if (-not @($settings.vdd_settings.global.g_refresh_rate | Where-Object { [string]$_ -eq '60' }).Count) {
        $refresh = $settings.CreateElement('g_refresh_rate')
        $refresh.InnerText = '60'
        $null = $settings.vdd_settings.global.AppendChild($refresh)
    }
    foreach ($mode in @(@('2560','1440'), @('2560','1600'), @('3840','2160'))) {
        $present = @($settings.vdd_settings.resolutions.resolution | Where-Object {
            [string]$_.width -eq $mode[0] -and [string]$_.height -eq $mode[1]
        }).Count -gt 0
        if (-not $present) {
            $resolution = $settings.CreateElement('resolution')
            foreach ($item in @(@('width',$mode[0]), @('height',$mode[1]), @('refresh_rate','30'))) {
                $element = $settings.CreateElement($item[0])
                $element.InnerText = $item[1]
                $null = $resolution.AppendChild($element)
            }
            $null = $settings.vdd_settings.resolutions.AppendChild($resolution)
        }
    }
    $writerSettings = New-Object Xml.XmlWriterSettings
    $writerSettings.Indent = $true
    $writerSettings.Encoding = New-Object Text.UTF8Encoding($false)
    $writer = [Xml.XmlWriter]::Create($vddConfig, $writerSettings)
    try { $settings.Save($writer) } finally { $writer.Dispose() }

    if (-not $publisherWasPresent) {
        $imported = Import-Certificate -FilePath $certificatePath -CertStoreLocation Cert:\LocalMachine\TrustedPublisher
        if (-not $imported -or $imported.Thumbprint -ne $expectedThumbprint) { throw 'TrustedPublisher import failed' }
    }
    if (-not (Get-ChildItem Cert:\LocalMachine\TrustedPublisher | Where-Object Thumbprint -eq $expectedThumbprint)) {
        throw 'Approved certificate is absent after import'
    }
    if ($rootWasPresent -ne [bool](Get-ChildItem Cert:\LocalMachine\Root | Where-Object Thumbprint -eq $expectedThumbprint)) {
        throw 'Trusted Root store unexpectedly changed'
    }

    $setupLog = 'C:\Windows\INF\setupapi.dev.log'
    $setupLineCount = @(Get-Content -LiteralPath $setupLog -ErrorAction SilentlyContinue).Count
    $installStarted = Get-Date
    $mutationStarted = $true
    $installOutput = (& $nefcon install $inf 'Root\MttVDD' --no-duplicates 2>&1 | Out-String)
    $installExitCode = $LASTEXITCODE

    $deadline = (Get-Date).AddSeconds(60)
    do {
        Start-Sleep -Milliseconds 500
        $publishedInf = Find-MttPublishedInf
        $roots = @(Get-MttRootDevices)
        $children = @(Get-MttChildMonitors)
        $activeChildren = @(Get-ActiveMttMonitors)
        $rootHealthy = $roots.Count -eq 1 -and [string]$roots[0].Status -eq 'OK' -and [int]$roots[0].Problem -eq 0
        $childHealthy = $children.Count -ge 1 -and @($children | Where-Object { [string]$_.Status -eq 'OK' -and [int]$_.Problem -eq 0 }).Count -ge 1
    } while ((-not $publishedInf -or -not $rootHealthy -or -not $childHealthy -or $activeChildren.Count -lt 1) -and (Get-Date) -lt $deadline)

    $setupSlice = @(Get-Content -LiteralPath $setupLog -ErrorAction SilentlyContinue | Select-Object -Skip $setupLineCount)
    $setupSlice | Set-Content -LiteralPath (Join-Path $backupRoot 'setupapi-attempt.log') -Encoding UTF8
    $rebootRequested = [bool](@($setupSlice | Where-Object { $_ -match '(?i)restart required|0x00000bc3|ERROR_SUCCESS_REBOOT_REQUIRED' }).Count)
    $setupText = $setupSlice -join "`n"
    $packageSignatureValidated = [bool](
        $setupText -match '(?i)Driver package catalog is valid' -and
        $setupText -match '(?i)Success: File is signed in Authenticode\(tm\) catalog' -and
        $setupText -match '(?i)Signer Name\s*=\s*SignPath Foundation' -and
        $setupText -match '(?i)DRIVERSTORE IMPORT VALIDATE: exit\(0x00000000\)' -and
        $setupText -match '(?i)Publish Driver Package: exit\(0x00000000\)' -and
        $setupText -match '(?i)Exit status: SUCCESS'
    )
    $rootInstanceIds = @($roots | Select-Object -ExpandProperty InstanceId)
    $rootInfPaths = @($roots | ForEach-Object { [string](Get-DevicePropertyValue $_ 'DEVPKEY_Device_DriverInfPath') })
    $signedDriver = @(Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | Where-Object {
        ($publishedInf -and [string]$_.InfName -ieq $publishedInf) -or
        ($rootInstanceIds -contains [string]$_.DeviceID)
    } | Select-Object DeviceName,DeviceID,DeviceClass,Manufacturer,DriverProviderName,DriverVersion,DriverDate,InfName,IsSigned,Signer)

    $genuineFailure = $false
    $failureReason = $null
    if ($installExitCode -ne 0 -and -not $rebootRequested) {
        $genuineFailure = $true; $failureReason = "nefcon exited $installExitCode without a reboot-required result"
    } elseif (-not $publishedInf) {
        $genuineFailure = $true; $failureReason = 'MttVDD package was not published'
    } elseif ($roots.Count -ne 1) {
        $genuineFailure = $true; $failureReason = "Expected one Root\\MttVDD device, found $($roots.Count)"
    } elseif (-not $rootHealthy) {
        $genuineFailure = $true; $failureReason = "Root\\MttVDD is not healthy: status=$($roots[0].Status), problem=$($roots[0].Problem)"
    } elseif ($rootInfPaths.Count -lt 1 -or -not @($rootInfPaths | Where-Object { $_ -ieq $publishedInf }).Count) {
        $genuineFailure = $true; $failureReason = "Root\\MttVDD is not bound to the published INF $publishedInf"
    } elseif (-not $packageSignatureValidated) {
        $genuineFailure = $true; $failureReason = 'SetupAPI did not record complete signed-catalog validation and package publication success'
    } elseif ($children.Count -lt 1 -and -not $rebootRequested) {
        $genuineFailure = $true; $failureReason = 'MTT1337 child monitor did not enumerate and Windows did not request a reboot'
    } elseif ($children.Count -ge 1 -and -not $childHealthy -and -not $rebootRequested) {
        $genuineFailure = $true; $failureReason = 'MTT1337 child monitor enumerated with a non-zero problem state'
    }

    if ($genuineFailure) {
        Invoke-Rollback
        $failure = [pscustomobject][ordered]@{
            installed = $false
            rolled_back = $true
            genuine_failure = $true
            error = $failureReason
            backup_directory = $backupRoot
            install_exit_code = $installExitCode
            reboot_requested = $rebootRequested
            root_devices = @($roots | Select-Object FriendlyName,InstanceId,Status,Problem)
            child_monitors = @($children | Select-Object FriendlyName,InstanceId,Status,Problem)
            virtual_monitor_active = $activeChildren.Count -ge 1
            active_virtual_monitors = $activeChildren
        }
        $failure | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $backupRoot 'install-result.json') -Encoding UTF8
        Write-Result $failure
        exit 2
    }

    $outcome = if ($rootHealthy -and $childHealthy -and $activeChildren.Count -ge 1) { 'pnp_and_display_active' } else { 'pnp_healthy_reboot_or_activation_pending' }
    $result = [pscustomobject][ordered]@{
        installed = $true
        rolled_back = $false
        outcome = $outcome
        backup_directory = $backupRoot
        install_started_utc = $installStarted.ToUniversalTime().ToString('o')
        install_exit_code = $installExitCode
        install_output = $installOutput.Trim()
        reboot_requested = $rebootRequested
        package_published = [bool]$publishedInf
        published_inf = $publishedInf
        root_present = $roots.Count -eq 1
        root_started = $rootHealthy
        root_devices = @($roots | ForEach-Object { [pscustomobject]@{ friendly_name=$_.FriendlyName; instance_id=$_.InstanceId; status=[string]$_.Status; problem=[int]$_.Problem; hardware_ids=@(Get-HardwareIds $_) } })
        child_present = $children.Count -ge 1
        child_started = $childHealthy
        child_monitors = @($children | ForEach-Object { [pscustomobject]@{ friendly_name=$_.FriendlyName; instance_id=$_.InstanceId; status=[string]$_.Status; problem=[int]$_.Problem; hardware_ids=@(Get-HardwareIds $_) } })
        virtual_monitor_active = $activeChildren.Count -ge 1
        active_virtual_monitors = $activeChildren
        signed_driver = $signedDriver
        signed_driver_record_available = $signedDriver.Count -ge 1
        signed_driver_metadata_only = $true
        package_signature_validated = $packageSignatureValidated
        root_driver_inf_paths = $rootInfPaths
        archive_sha256 = $archiveHash
        catalog_sha256 = $catalogHash
        signer_thumbprint = $signature.SignerCertificate.Thumbprint
        trusted_publisher_present = [bool](Get-ChildItem Cert:\LocalMachine\TrustedPublisher | Where-Object Thumbprint -eq $expectedThumbprint)
        trusted_root_present = [bool](Get-ChildItem Cert:\LocalMachine\Root | Where-Object Thumbprint -eq $expectedThumbprint)
        vdd_config_sha256 = (Get-FileHash -LiteralPath $vddConfig -Algorithm SHA256).Hash
    }
    Get-CurrentState | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $backupRoot 'state-after-install.json') -Encoding UTF8
    $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $backupRoot 'install-result.json') -Encoding UTF8
    Write-Result $result
} catch {
    $message = $_.Exception.Message
    $retainInstalledDriver = $false
    if ($mutationStarted) {
        $catchPublishedInf = Find-MttPublishedInf
        $catchRoots = @(Get-MttRootDevices)
        $retainInstalledDriver = [bool](
            $catchPublishedInf -and
            $catchRoots.Count -eq 1 -and
            [string]$catchRoots[0].Status -eq 'OK' -and
            [int]$catchRoots[0].Problem -eq 0
        )
        if (-not $retainInstalledDriver) { Invoke-Rollback }
    }
    $failure = [pscustomobject][ordered]@{
        installed = $retainInstalledDriver
        rolled_back = [bool]($mutationStarted -and -not $retainInstalledDriver)
        genuine_failure = [bool]($mutationStarted -and -not $retainInstalledDriver)
        error = $message
        backup_directory = $backupRoot
    }
    $failure | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $backupRoot 'install-result.json') -Encoding UTF8
    Write-Result $failure
    exit 2
}
