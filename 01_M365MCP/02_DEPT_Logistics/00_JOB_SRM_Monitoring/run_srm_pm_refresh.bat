@echo off

chcp 65001 > nul

setlocal

title SRM PM Refresh



cd /d "%~dp0"

if not exist "logs" mkdir "logs"



set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"



:: #330/#392/#396 Manual PM refresh after a late SRM order change:

:: Opens a window showing progress & per-supplier result (or refusal reason), logs JSON to logs\srm_pipeline.log, and pauses.

"%PYTHON_EXE%" -X utf8 run_srm_pipeline.py --slot PM --refresh --console %*

set EXIT_CODE=%ERRORLEVEL%



echo.

if /I not "%SRM_NO_PAUSE%"=="1" pause

exit /b %EXIT_CODE%

