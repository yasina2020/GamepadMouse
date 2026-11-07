@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Gamepad Mouse

rem Prefer pythonw.exe (no console window); fall back to python.exe.
set "PYW="
for /f "delims=" %%i in ('where python 2^>nul') do (
  if exist "%%~dpipythonw.exe" (
    set "PYW=%%~dpipythonw.exe"
    goto :found
  )
)

:found
if defined PYW (
  start "" "!PYW!" "app.py" %*
  exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.9+ ^(64-bit^) is required but was not found in PATH.
  echo Please install Python from https://www.python.org and run this again.
  pause
  exit /b 1
)
start "" python "app.py" %*
exit /b 0
