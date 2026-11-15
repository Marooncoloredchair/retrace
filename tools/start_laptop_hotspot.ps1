$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime]
$null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1"
})[0]
function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}
function AwaitAction($WinRtAction) {
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and -not $_.IsGenericMethod
    })[0]
    $netTask = $asTask.Invoke($null, @($WinRtAction))
    $netTask.Wait(-1) | Out-Null
}
$text = Get-Content -Raw (Join-Path $PSScriptRoot "..\firmware\xiao_sense_usb_motor\wifi_config.h")
$ssid = [regex]::Match($text, 'RETRACE_WIFI_SSID "(.*)"').Groups[1].Value
$pass = [regex]::Match($text, 'RETRACE_WIFI_PASSWORD "(.*)"').Groups[1].Value
if (-not $ssid -or -not $pass) { Write-Output "MISSING_HOTSPOT_CONFIG"; exit 2 }
$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $profile) { Write-Output "NO_PROFILE"; exit 2 }
$manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
$cfg = $manager.GetCurrentAccessPointConfiguration()
$cfg.Ssid = $ssid
$cfg.Passphrase = $pass
try { $cfg.Band = [Windows.Networking.NetworkOperators.TetheringWiFiBand]::TwoPointFourGigahertz } catch {}
AwaitAction ($manager.ConfigureAccessPointAsync($cfg))
$result = Await $manager.StartTetheringAsync() ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
Write-Output ("START " + $result.Status)
Write-Output ("STATE " + $manager.TetheringOperationalState)
