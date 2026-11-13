@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

:: Execute Backlog Pipeline
"%PYTHON_EXE%" backlog_pipeline.py %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
