@echo off
echo Starting Momentum Bot + Dashboard...
echo Momentum: top 10 gap-up movers at 9:35 AM
echo Mean Reversion: disabled (run start_meanreversion.bat to enable)
echo.
start "Momentum Bot" python bot.py
start "Dashboard" python serve.py
timeout /t 2 /nobreak >nul
start http://localhost:5000/dashboard.html
