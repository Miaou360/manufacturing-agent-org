@echo off
chcp 65001 > nul
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" hourly_gi_pipeline.py
