@echo off
echo Starting MEAN REVERSION BOT only...
echo Strategy: RSI under 30 + below 20-day SMA, entries at 11am and 1pm, $40 target per position
echo.
start "Mean Reversion Bot" python mean_reversion.py
start "Dashboard" python serve.py
timeout /t 2 /nobreak >nul
start http://localhost:5000/dashboard.html
