# Registers a Windows scheduled task (for this user only, no admin needed) that keeps Orbit running:
# it starts Orbit when you log in and checks every 10 minutes, restarting it if it has stopped.
# Remove it with .\scripts\uninstall-autostart.ps1
. "$PSScriptRoot\_common.ps1"
$TaskName = "Orbit Trading Lab"
$script = Join-Path $PSScriptRoot "ensure-running.ps1"
$ps = (Get-Command powershell.exe).Source
$action = New-ScheduledTaskAction -Execute $ps -WorkingDirectory $Root `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$user = "$env:USERDOMAIN\$env:USERNAME"
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$atLogon.Delay = "PT1M"  # give the network a minute after login
$every10 = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $every10) -Settings $settings `
  -Principal $principal -Description "Keeps Orbit Trading Lab (paper trading) running. Created by scripts\install-autostart.ps1." -Force | Out-Null
Write-Host "Scheduled task '$TaskName' installed: starts Orbit at login and checks every 10 minutes." -ForegroundColor Green
Write-Host "A deliberate .\scripts\stop.ps1 is respected until you run .\scripts\start.ps1 again."
