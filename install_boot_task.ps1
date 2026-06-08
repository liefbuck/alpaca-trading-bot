# Installs a SYSTEM "at startup" scheduled task so the trading watchdog launches
# at boot, BEFORE any user logs in -- this is what makes the bot survive an
# unattended overnight reboot (e.g. a forced Windows Update).
#
# EASIEST: double-click  install_boot_task.bat  and click YES on the UAC prompt.
# This script self-elevates, so running it directly works too. It writes the
# outcome to  install_boot_task.log  so the result is visible even if the window
# closes.
#
# The per-user logon task (ClaudeTrading-Watchdog) already covers the normal
# logged-in case with live toasts. Under an unattended-reboot session the bot
# runs in session 0 (no toast popups) but trading + step_alerts.log work. The
# watchdog's singleton lock keeps the two tasks from clashing.

$LogFile = Join-Path $PSScriptRoot "install_boot_task.log"
function Log($m) {
    Add-Content -Path $LogFile -Value ("$((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))  $m")
    Write-Host $m
}

# --- Self-elevate if we are not already an administrator ---
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Log "Not elevated - asking for admin via UAC (click YES)."
    try {
        Start-Process powershell.exe -Verb RunAs -ErrorAction Stop -ArgumentList @(
            '-NoProfile','-ExecutionPolicy','Bypass','-File',('"{0}"' -f $PSCommandPath)
        )
        Log "UAC accepted - an elevated window opened to finish the install."
    } catch {
        Log "ELEVATION CANCELLED OR FAILED: $($_.Exception.Message)"
        Write-Host "Could not get administrator rights. Nothing was changed." -ForegroundColor Red
        Read-Host "Press Enter to close"
    }
    return
}

# --- Elevated from here ---
$ErrorActionPreference = "Stop"
try {
    $watchdog = Join-Path $PSScriptRoot "watchdog.ps1"
    if (-not (Test-Path $watchdog)) { throw "watchdog.ps1 not found in $PSScriptRoot" }

    $action    = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$watchdog`""
    $trigger   = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)

    Register-ScheduledTask -TaskName "ClaudeTrading-Watchdog-Boot" -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

    Log "SUCCESS: registered SYSTEM at-startup task 'ClaudeTrading-Watchdog-Boot'."
    Write-Host ""
    Write-Host "SUCCESS - the bot will now start at boot even with no user logged in." -ForegroundColor Green
}
catch {
    Log "FAILED: $($_.Exception.Message)"
    Write-Host ""
    Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
}
finally {
    Write-Host ""
    Read-Host "Press Enter to close"
}
