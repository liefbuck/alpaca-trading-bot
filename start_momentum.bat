@echo off
echo Starting MOMENTUM BOT only...
echo Strategy: top 5 gap-up movers at 9:35 AM, $40 target per position
echo.
start "Momentum Bot" python bot.py
start "Dashboard" python serve.py
timeout /t 2 /nobreak >nul
start http://localhost:5000/dashboard.html
