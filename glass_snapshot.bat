@echo off
setlocal
title Glass Snapshot
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Taking a Glass snapshot (screenshot + your code + versions)...
echo.
"%PY%" "%~dp0core\glass_snapshot.py"
echo.
echo Opening the snapshot folder so you can review it...
if exist "%~dp0glass_snapshot" start "" "%~dp0glass_snapshot"
echo.
pause
