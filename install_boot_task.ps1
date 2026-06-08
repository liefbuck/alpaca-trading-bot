# Registers a SYSTEM "at startup" scheduled task so the trading watchdog launches
# at boot, BEFORE any user logs in. This is what makes the bot survive an
# unattended overnight reboot (e.g. a forced Windows Update) instead of sitting
# dead until someone logs in.
#
# EASIEST: just double-click  install_boot_task.bat  and click YES on the UAC
# prompt. That self-elevates and runs this script for you.
#
# (Manual alternative: right-click Windows PowerShell -> Run as administrator,
#  then:  & "C:\ClaudeTrading\install_boot_task.ps1")
#
# The per-user logon task (ClaudeTrading-Watchdog) still handles the normal
# logged-in case and shows live toast popups. Under an unattended-reboot session
# the bot runs in session 0, so toasts won't pop, but trading and step_alerts.log
# work normally. The watchdog's singleton lock keeps the two tasks from clashing.
$ErrorActionPreference = "Stop"

try {
    $identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Not elevated. Double-click install_boot_task.bat (and click YES), or run this from an Administrator PowerShell."
    }

    $watchdog = Join-Path $PSScriptRoot "watchdog.ps1"
    if (-not (Test-Path $watchdog)) { throw "watchdog.ps1 not found next to this script ($PSScriptRoot)." }

    $action    = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$watchdog`""
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName "ClaudeTrading-Watchdog-Boot" -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

    Write-Host ""
    Write-Host "SUCCESS: registered SYSTEM at-startup task 'ClaudeTrading-Watchdog-Boot'." -ForegroundColor Green
    Write-Host "The bot will now start at boot even with no user logged in."
}
catch {
    Write-Host ""
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "Press Enter to close"
}
