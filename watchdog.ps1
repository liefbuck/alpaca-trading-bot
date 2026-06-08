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
function Find-RunningPid($script) {
    $hits = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
              Where-Object { $_.ExecutablePath -eq $py -and $_.CommandLine -like "*$script*" })
    if ($hits.Count -ge 1) { return $hits[0].ProcessId }
    return $null
}

# Singleton guard: never let two watchdogs manage the bots at once (e.g. a
# SYSTEM at-startup instance plus a per-user logon instance after an unattended
# reboot). The first to start owns a lock file; a later instance that finds a
# LIVE watchdog already recorded there exits immediately. Without this, two
# watchdogs could race on the pid files and double-spawn.
$lockFile = Join-Path $baseDir ".watchdog.lock"
function Test-WatchdogAlive($procId) {
    if (-not $procId) { return $false }
    try { $p = Get-Process -Id $procId -ErrorAction Stop } catch { return $false }
    return ($p.ProcessName -match 'powershell|pwsh')
}
if (Test-Path $lockFile) {
    $otherPid = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($otherPid -and ("$otherPid" -ne "$PID") -and (Test-WatchdogAlive $otherPid)) {
        Write-Log "Another watchdog (PID $otherPid) already running - instance $PID exiting."
        return
    }
}
Set-Content -Path $lockFile -Value $PID
Start-Sleep -Milliseconds 300   # settle a near-simultaneous start race
$owner = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
if ("$owner" -ne "$PID") {
    Write-Log "Lost watchdog lock race to PID $owner - instance $PID exiting."
    return
}

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
    Start-Sleep -Seconds 60
}
