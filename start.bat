@echo off
echo Stopping any running bots...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM python3.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul

echo Starting Momentum Bot + Dashboard...
echo Strategy: top gap-up movers from S&P 500 + Nasdaq 100 at 9:33 AM
echo.
start "Momentum Bot" python bot.py
start "Dashboard" python serve.py
ping -n 3 127.0.0.1 >nul
start http://localhost:5000/dashboard.html
