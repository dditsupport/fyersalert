@echo off
REM ============================================================
REM  BB Alert System - start the web dashboard
REM  Double-click, or run from Task Scheduler at log on.
REM ============================================================
title BB Alert Dashboard
cd /d "%~dp0"

REM Use the project virtual environment if present
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

if not exist logs mkdir logs

REM Open the dashboard in the browser once the server is up
start "" cmd /c "timeout /t 6 /nobreak >nul & start http://127.0.0.1:5000"

echo Starting BB Alert Dashboard on http://127.0.0.1:5000
echo Log: %~dp0logs\dashboard.log
echo Close this window to stop the dashboard (and the monitor).
"%PY%" -u web_app.py >> logs\dashboard.log 2>&1
