@echo off
setlocal
title Glass DRM Diagnostic
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo Running Glass DRM diagnostic...
echo.
"%PY%" "%~dp0core\drm_diag.py"
echo.
echo ------------------------------------------------------------
echo Copy the VERDICT line above and send it over.
echo ------------------------------------------------------------
pause
