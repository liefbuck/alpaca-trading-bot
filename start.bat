@echo off
echo Starting momentum bot (9:35 AM entries)...
start "Momentum Bot" python bot.py

echo Starting mean reversion bot (11:00 AM + 1:00 PM entries)...
start "Mean Reversion Bot" python mean_reversion.py

echo Starting dashboard...
start "Dashboard" python serve.py

timeout /t 2 /nobreak >nul
start http://localhost:5000/dashboard.html

echo.
echo All systems running.
echo Momentum bot:      targets top 5 gap-up movers at open
echo Mean reversion bot: targets 5 oversold stocks at 11am and 1pm
echo Dashboard:         http://localhost:5000/dashboard.html
