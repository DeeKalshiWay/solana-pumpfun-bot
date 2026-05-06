@echo off
REM Double-click to start the bot in 24/7 watchdog mode.
REM Closes the console = stops the bot. For true background operation,
REM run install_autostart.ps1 instead.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "run_forever.ps1"
pause
