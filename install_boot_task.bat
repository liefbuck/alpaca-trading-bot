@echo off
title ClaudeTrading - install boot auto-start
echo.
echo  This makes the trading bot start at EVERY Windows boot, even after an
echo  unattended overnight reboot with nobody logged in.
echo.
echo  A Windows security (UAC) prompt will appear in a moment - click YES.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_boot_task.ps1"
