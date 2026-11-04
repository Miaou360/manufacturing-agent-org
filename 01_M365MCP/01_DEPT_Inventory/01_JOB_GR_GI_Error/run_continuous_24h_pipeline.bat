@echo off
chcp 65001 > nul
setlocal

REM Set working directory
cd /d "%~dp0"

REM Python executable
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

REM Run 24/7 Continuous Autonomous GI Error Pipeline
REM 08:00 ~ 17:00 EST: 30-min intervals
REM 17:01 ~ 07:59 EST: 1-hour intervals
"%PYTHON_EXE%" hourly_gi_pipeline.py --daemon %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
