# Registers a SYSTEM "at startup" scheduled task so the trading watchdog launches
# at boot, BEFORE any user logs in. This is what makes the bot survive an
# unattended overnight reboot (e.g. a forced Windows Update) instead of sitting
# dead until someone logs in.
#
# Run ONCE, elevated:
#   1. Right-click Windows PowerShell -> "Run as administrator"
#   2.  & "C:\ClaudeTrading\install_boot_task.ps1"
#
# The per-user logon task (ClaudeTrading-Watchdog) still handles the normal
# logged-in case and shows live toast popups. Under an unattended-reboot session
# the bot runs in session 0, so toasts won't pop, but trading and step_alerts.log
# work normally. The watchdog's singleton lock keeps the two tasks from clashing.
$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This must be run from an elevated (Administrator) PowerShell."
    exit 1
}

$watchdog = Join-Path $PSScriptRoot "watchdog.ps1"
if (-not (Test-Path $watchdog)) { Write-Error "watchdog.ps1 not found next to this script."; exit 1 }

$action    = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$watchdog`""
$trigger   = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "ClaudeTrading-Watchdog-Boot" -Action $action `
    -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered SYSTEM at-startup task 'ClaudeTrading-Watchdog-Boot'."
Write-Host "The bot will now start at boot even with no user logged in."
