@echo off
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
python -X utf8 d114_stage_flow_shadow.py --mode ingest_only >> "logs\d114_shadow.log" 2>&1
