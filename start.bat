@echo off
REM Single source of truth: delegate to start.ps1, which stops only THIS bot's
REM processes (not every python.exe), starts bot + dashboard, and launches the
REM watchdog (which in turn keeps step_watch alive). The old version here killed
REM all python.exe and never started the watchdog or step_watch.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1"
