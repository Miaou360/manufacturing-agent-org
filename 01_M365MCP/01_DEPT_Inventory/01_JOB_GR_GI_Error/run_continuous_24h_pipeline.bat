@echo off
chcp 65001 > nul
setlocal

REM Set working directory
cd /d "%~dp0"

REM Python executable
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

REM Run 24/7 Continuous Autonomous GI Error Pipeline
REM 00:00 ~ 23:00 EST: Hourly on the hour (:00, 24h; 05:30-06:15 guard preserved)
"%PYTHON_EXE%" hourly_gi_pipeline.py --daemon %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
