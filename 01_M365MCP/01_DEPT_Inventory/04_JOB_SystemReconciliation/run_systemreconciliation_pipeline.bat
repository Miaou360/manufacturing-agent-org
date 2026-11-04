@echo off
chcp 65001 > nul
title 04_JOB_SystemReconciliation Pipeline

cd /d "%~dp0"
echo ====================================================================
echo  [04_JOB_SystemReconciliation] Daily WMS vs SAP Accuracy Pipeline
echo ====================================================================

set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

"%PYTHON_EXE%" run_systemreconciliation.py --system IWMS --email %*

if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Pipeline encountered an error! Exit code: %ERRORLEVEL%
    exit /b %ERRORLEVEL%
)

echo [OK] 04_JOB_SystemReconciliation Pipeline completed successfully.
exit /b 0
