# Trading bot watchdog - restarts bot.py, serve.py, step_watch.py if they die.
#
# Hardened against false-negative duplicate spawns. The old version decided a
# script was dead whenever Get-CimInstance returned no CommandLine match -- but
# Win32_Process.CommandLine intermittently comes back $null (a known Windows
# quirk), so a *running* process would look absent and get relaunched, piling up
# duplicates over time. For bot.py that risks double orders.
#
# This version tracks each script by a PID file and checks liveness with
# Get-Process -Id (reliable, never false-negative). It only relaunches after
# also failing to ADOPT an existing instance (with a retry), so a transient
# null-CommandLine read can never trigger a duplicate launch.

$py      = "C:\Users\liefb\AppData\Local\Python\pythoncore-3.14-64\python.exe"
# Derive the project dir from the script's own location so the watchdog works
# wherever the repo lives, with no hardcoded (and sync-prone) absolute path.
$baseDir = $PSScriptRoot
$log     = "$baseDir\watchdog.log"
$scripts = @("bot.py", "serve.py", "step_watch.py")

function Write-Log($msg) {
    $ts = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content $log "$ts  $msg"
}

function Get-PidFile($script) { Join-Path $baseDir (".watchdog_" + $script + ".pid") }

# True only if $procId is a live python.exe (guards against PID reuse).
function Test-Alive($procId) {
    if (-not $procId) { return $false }
    try { $p = Get-Process -Id $procId -ErrorAction Stop } catch { return $false }
    return ($p.ProcessName -eq 'python')
}

# Best-effort: find a running instance of $script by command line. May miss when
# CommandLine is $null, so a miss is NOT proof the process is dead.
#
# BOUNDED: the Win32_Process enumeration is a CIM/WMI call that can hang if the
# WMI provider wedges. A hang here used to stall the whole sweep, so a dead bot.py
# would never be relaunched (this happened ~20 min before the open on 2026-06-11).
# We run the query in a child job and ABANDON it if it does not return within the
# timeout, reporting "not found" so the caller relaunches instead of blocking
# forever. Relaunching always wins over a wedged discovery query.
function Find-RunningPid($script) {
    $job = Start-Job -ScriptBlock {
        param($pyPath, $scriptName)
        $hits = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
                  Where-Object { $_.ExecutablePath -eq $pyPath -and $_.CommandLine -like "*$scriptName*" })
        if ($hits.Count -ge 1) { $hits[0].ProcessId } else { $null }
    } -ArgumentList $py, $script
    $finished = Wait-Job $job -Timeout 15
    if (-not $finished) {
        Write-Log "Find-RunningPid($script): CIM query timed out (15s) - abandoning, will relaunch"
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        return $null
    }
    $result = Receive-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    return $result
}

# Singleton guard: never let two watchdogs manage the bots at once (e.g. a SYSTEM
# at-startup instance plus a per-user logon instance after an unattended reboot).
#
# Implemented as an OS-ENFORCED EXCLUSIVE FILE LOCK: we open .watchdog.lock with
# FileShare.Read (others may READ it for diagnostics, but no second WRITER can open
# it) and HOLD the handle for the whole life of the watchdog. The OS enforces this
# across the SYSTEM/user security boundary -- unlike the old guard, which compared
# the rival's CommandLine via CIM. That came back $null across the boundary, so a
# SYSTEM boot-watchdog and a user logon-watchdog BOTH ran and dueled on the account
# (2026-06-11). The OS releases the handle automatically when the process dies, so a
# restart re-acquires cleanly with no stale-file handling.
$lockFile = Join-Path $baseDir ".watchdog.lock"
$script:lockStream = $null
for ($attempt = 1; ($attempt -le 5) -and (-not $script:lockStream); $attempt++) {
    try {
        $script:lockStream = [System.IO.File]::Open(
            $lockFile,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::Read)
    } catch {
        # Sharing violation = another watchdog holds it (or one we just replaced has
        # not released yet). Retry a few times to cover a kill-then-relaunch handoff.
        Start-Sleep -Seconds 2
    }
}
if (-not $script:lockStream) {
    Write-Log "Another watchdog holds the lock - instance $PID exiting."
    return
}
# Record our PID in the locked file for diagnostics. The stream stays OPEN (never
# disposed) so the exclusive lock is held for the watchdog's entire lifetime.
try {
    $script:lockStream.SetLength(0)
    $b = [System.Text.Encoding]::ASCII.GetBytes("$PID")
    $script:lockStream.Write($b, 0, $b.Length)
    $script:lockStream.Flush()
} catch {}

Write-Log "Watchdog started (hardened/pid-file/singleton)"

while ($true) {
    # Guard the whole sweep: a transient failure (CIM hiccup, Start-Process throw)
    # must never crash the watchdog itself -- nothing would restart the restarter.
    try {
        foreach ($script in $scripts) {
            $pidFile = Get-PidFile $script
            $procId  = $null
            if (Test-Path $pidFile) {
                $procId = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            }

            if (Test-Alive $procId) { continue }   # tracked process alive -> nothing to do

            # Tracked PID dead/unknown. Try to adopt an existing instance before
            # relaunching, with one retry, so a transient null read can't duplicate.
            $found = Find-RunningPid $script
            if (-not $found) { Start-Sleep -Seconds 2; $found = Find-RunningPid $script }

            if ($found) {
                Set-Content -Path $pidFile -Value $found
                Write-Log "ADOPT: $script already running (PID $found) - tracking"
                continue
            }

            # Genuinely absent after retries -> relaunch and record the new PID.
            $proc = Start-Process $py -ArgumentList $script -WorkingDirectory $baseDir -WindowStyle Normal -PassThru
            Set-Content -Path $pidFile -Value $proc.Id
            Write-Log "RESTART: $script not running - relaunched (PID $($proc.Id))"
        }
    } catch {
        Write-Log "WATCHDOG ERROR (continuing): $($_.Exception.Message)"
    }
    # Heartbeat: a completed sweep stamps this file. An external monitor (or a
    # future session) can detect a wedged watchdog by a stale stamp. Because the
    # CIM discovery above is now bounded, a sweep can no longer block indefinitely,
    # so this stamp advances every ~60s while the watchdog is healthy.
    Set-Content -Path (Join-Path $baseDir ".watchdog.heartbeat") -Value (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 60
}
