@echo off
chcp 65001 > nul
setlocal

cd /d "%~dp0"
if not exist "logs" mkdir "logs"

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

:: #351 / #377 / #378 Intraday WMS GR Truck Delivery Check (10m cadence, 07:40 -> 05:59 next day, Mon-Sat).
:: Test mode only (config\delivery_check.json: mode='test', user only, enabled=true kill switch).
"%PYTHON_EXE%" -X utf8 run_delivery_check.py --send-test-mail %* >> "logs\delivery_check_10m.log" 2>&1

set EXIT_CODE=%ERRORLEVEL%
exit /b %EXIT_CODE%
