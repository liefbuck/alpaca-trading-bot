@echo off
REM Delegates to start.ps1 (single source of truth) so the bot always comes up
REM supervised by the watchdog with step_watch running. The old version started
REM bot + dashboard only, leaving the bot unsupervised with no entry-window pings.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0start.ps1"
