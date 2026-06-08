# Trading bot launcher -- kills watchdog + bots, then starts ONLY the watchdog,
# which is the single source of truth for spawning bot.py / serve.py /
# step_watch.py. A previous version also launched bot.py and serve.py directly
# here, using `Get-Command python` -- which resolves to a DIFFERENT interpreter
# than the watchdog hardcodes. The watchdog only adopts processes whose
# ExecutablePath matches its own, so it could not see these and spawned its own
# duplicate set: two bots, two serves -> double-order risk. Letting the watchdog
# own all launching removes both the duplication and the interpreter mismatch.
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# Stop watchdog first so it does not re-spawn processes we are about to kill.
Write-Host "Stopping watchdog..."
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -like "*watchdog.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# Kill only THIS bot's python processes (match the script name on the command
# line). The old version killed every python.exe on the machine, taking out any
# unrelated Python the user happened to be running.
Write-Host "Stopping any running bots..."
Get-CimInstance Win32_Process -Filter "name='python.exe'" |
    Where-Object { $_.CommandLine -match 'bot\.py|serve\.py|step_watch\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# Clear trading_halted from state so a manual restart always starts fresh.
$stateFile = "$BaseDir\bot_state.json"
if (Test-Path $stateFile) {
    try {
        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        $today = (Get-Date).ToString("yyyy-MM-dd")
        # Only clear trading_halted if it was set on a PREVIOUS day.
        # If it was set TODAY (e.g. user manually halted), preserve it.
        if ($state.trading_halted -eq $true -and $state.date -ne $today) {
            $state.trading_halted = $false
            $state | ConvertTo-Json -Compress | Set-Content $stateFile -Encoding utf8
            Write-Host "  Cleared stale trading_halted flag from previous session."
        } elseif ($state.trading_halted -eq $true) {
            Write-Host "  trading_halted is set for today - preserving halt."
        }
    } catch {
        Write-Host "  (Could not read state file - starting fresh)"
    }
}

# Clear stale watchdog pid files so it relaunches a fresh set rather than trying
# to adopt the PIDs we just killed.
Remove-Item (Join-Path $BaseDir ".watchdog_*.pid") -Force -ErrorAction SilentlyContinue

Write-Host "Starting Momentum Bot + Dashboard..."
Write-Host "  Strategy: top gap-up movers from S&P 500 + Nasdaq 100 at 9:33 AM"

# Start ONLY the watchdog. It spawns bot.py, serve.py and step_watch.py itself,
# with the correct interpreter and pid-file tracking.
Write-Host "Starting watchdog (it launches bot.py, serve.py, step_watch.py)..."
Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$BaseDir\watchdog.ps1`"" -WorkingDirectory $BaseDir

# Give the watchdog time to bring the set up (it retries adoption ~2s per script).
Start-Sleep -Seconds 10
$procs = @(Get-CimInstance Win32_Process -Filter "name='python.exe'" |
           Where-Object { $_.CommandLine -match 'bot\.py|serve\.py|step_watch\.py' })
Write-Host "Running: $($procs.Count) bot process(es)"
foreach ($p in $procs) { Write-Host "  PID $($p.ProcessId): $($p.CommandLine)" }

Start-Process "http://localhost:5000"
