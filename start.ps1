# Trading bot launcher -- kills watchdog, kills bots, then starts fresh.
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = "C:\Users\liefb\AppData\Local\Python\pythoncore-3.14-64\python.exe" }

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

Write-Host "Starting Momentum Bot + Dashboard..."
Write-Host "  Strategy: top gap-up movers from S&P 500 + Nasdaq 100 at 9:33 AM"

Start-Process $py -ArgumentList "bot.py"   -WorkingDirectory $BaseDir -WindowStyle Normal
Start-Process $py -ArgumentList "serve.py" -WorkingDirectory $BaseDir -WindowStyle Normal
Start-Sleep -Seconds 2

# Restart watchdog now that the fresh processes are up.
Write-Host "Starting watchdog..."
Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$BaseDir\watchdog.ps1`"" -WorkingDirectory $BaseDir

Start-Sleep -Seconds 2
$procs = Get-CimInstance Win32_Process -Filter "name='python.exe'"
Write-Host "Running: $($procs.Count) Python process(es)"
foreach ($p in $procs) { Write-Host "  PID $($p.ProcessId): $($p.CommandLine)" }

Start-Process "http://localhost:5000"
