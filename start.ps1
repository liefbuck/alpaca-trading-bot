# Trading bot launcher — kills any existing Python processes first, then starts clean.
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "Stopping any running bots..."
Get-WmiObject Win32_Process -Filter "name='python.exe'" |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "Starting Momentum Bot + Dashboard..."
Write-Host "  Momentum:       top 10 gap-up movers at 9:35 AM"
Write-Host "  Mean Reversion: disabled (run start_meanreversion.bat to enable)"

Start-Process python -ArgumentList "bot.py"   -WorkingDirectory $BaseDir -WindowStyle Normal
Start-Process python -ArgumentList "serve.py" -WorkingDirectory $BaseDir -WindowStyle Normal
Start-Sleep -Seconds 3

$procs = Get-WmiObject Win32_Process -Filter "name='python.exe'"
Write-Host "Running: $($procs.Count) Python process(es)"
foreach ($p in $procs) { Write-Host "  PID $($p.ProcessId): $($p.CommandLine)" }

Start-Process "http://localhost:5000"
