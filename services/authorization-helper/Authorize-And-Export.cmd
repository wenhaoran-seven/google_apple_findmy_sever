@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto end
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto end
.venv\Scripts\python.exe -X utf8 check_dependencies.py
if errorlevel 1 goto end
.venv\Scripts\python.exe -X utf8 authorize.py --export-only
:end
pause
