@echo off
chcp 65001 > nul
setlocal

REM Set working directory
cd /d "%~dp0"

REM Python executable
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

REM D106: Standalone DB-only Daily GI Report (07:30 primary / 08:00 1-time retry)
"%PYTHON_EXE%" run_daily_gi_report.py %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
