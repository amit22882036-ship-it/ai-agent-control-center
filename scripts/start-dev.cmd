@echo off
rem Double-click to start the Control Center from this repository's current code (separate data, ports 8100/5180).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-dev.ps1"
if errorlevel 1 pause
