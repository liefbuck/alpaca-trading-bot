@echo off
REM Manual launcher for the mean-reversion bot. It is NOT supervised by the
REM watchdog and is independent of the momentum bot. serve.py and the dashboard
REM already run via the main watchdog, so this only starts mean_reversion, using
REM the same pythoncore interpreter the watchdog uses (bare "python" can resolve
REM to a different install that lacks the dependencies).
echo Starting MEAN REVERSION bot (manual). RSI under 30 + below 20-day SMA, 11am/1pm ET.
start "Mean Reversion Bot" "C:\Users\liefb\AppData\Local\Python\pythoncore-3.14-64\python.exe" "%~dp0mean_reversion.py"
start "" http://localhost:5000/dashboard.html
