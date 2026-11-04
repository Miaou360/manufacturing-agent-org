@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python mb51_collector.py --daily-0215 >> logs\mb51_daily_0215.log 2>&1
exit /b %ERRORLEVEL%
