@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"
if not exist "logs" mkdir "logs"

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

:: #330 SRM Monitoring PM slot 13:40, SRM delivery date = tomorrow; retries every 5 min until 14:00.
:: Mails: config\srm_mail.json (mode test = user only).
"%PYTHON_EXE%" -X utf8 run_srm_pipeline.py --slot PM %* >> "logs\srm_pipeline.log" 2>&1

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
