@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python run_daily_gi_report.py --check-post-0800
exit /b %ERRORLEVEL%

