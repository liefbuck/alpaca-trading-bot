@echo off
echo Starting MOMENTUM BOT only...
echo Strategy: top 10 gap-up movers from S&P 500 + Nasdaq 100 at 9:33 AM
echo.
start "Momentum Bot" python bot.py
start "Dashboard" python serve.py
timeout /t 2 /nobreak >nul
start http://localhost:5000/dashboard.html
