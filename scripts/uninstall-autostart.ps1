# Removes the "Orbit Trading Lab" scheduled task. Orbit itself keeps running until .\scripts\stop.ps1.
$TaskName = "Orbit Trading Lab"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  Write-Host "Scheduled task '$TaskName' removed."
} else { Write-Host "No '$TaskName' scheduled task was installed." }
