@echo off
cd /d "%~dp0"
title Gamepad Mouse - Console
echo Starting Gamepad Mouse with console output...
echo Close this window (or press Ctrl+C) to stop the service.
echo.
python "app.py" --no-browser %*
echo.
echo Service stopped.
pause
