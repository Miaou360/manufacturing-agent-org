@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if not exist logs mkdir logs
python d107_portal_drafter.py --mode save_temp_2b --send-test-email >> logs\d107_draft_0723.log 2>&1
exit /b %ERRORLEVEL%
