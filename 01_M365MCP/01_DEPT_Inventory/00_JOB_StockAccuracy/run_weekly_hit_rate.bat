@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

"%PYTHON_EXE%" weekly_hit_rate_notifier.py %*

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
