@echo off
title ClaudeTrading - install boot auto-start
echo.
echo  This makes the trading bot start automatically at EVERY Windows boot,
echo  even after an unattended overnight reboot with nobody logged in.
echo.
echo  When you press a key, a Windows security (UAC) prompt will appear.
echo  Click YES on it. That is the only step.
echo.
pause
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File %~dp0install_boot_task.ps1'"
echo.
echo  If you clicked YES and saw a green SUCCESS message, you're done.
echo  (Re-running this is harmless - it just re-registers the same task.)
echo.
pause
