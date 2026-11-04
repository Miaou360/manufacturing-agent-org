@echo off
chcp 65001 > nul
setlocal

:: Set working directory
cd /d "%~dp0"

:: Python executable
set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

:: Execute Daily Pipeline in Headless Mode (Lock-Screen Safe)
"%PYTHON_EXE%" daily_pipeline.py --headless %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
