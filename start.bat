@echo off
echo Starting trading bot...
start "Trading Bot" python bot.py

echo Starting dashboard...
start "Dashboard" python dashboard.py

echo.
echo Bot running in background.
echo Dashboard at http://localhost:5000
start http://localhost:5000/dashboard.html
