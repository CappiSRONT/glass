@echo off
setlocal EnableExtensions
cd /d "%~dp0" 2>nul
title Glass Launcher
color 0B

echo.
echo    ===========================================
echo            G L A S S
echo            L A U N C H E R
echo    ===========================================
echo.

REM --- make sure we're running from a fully extracted folder ------------------
if not exist "%~dp0core\launch.py" (
  color 0C
  echo   [X] Glass files are missing next to this launcher.
  echo.
  echo       It looks like you ran run.bat from INSIDE the .zip.
  echo       Please EXTRACT the whole Glass folder first ^(right-click the zip
  echo       -^> Extract All^), then run run.bat from the extracted folder.
  echo.
  pause
  exit /b 1
)

REM --- make sure this folder is actually writable - a protected/read-only ----
REM     extraction location (Program Files, a locked-down company folder, a
REM     network share) is the single most common real cause of "this needs
REM     admin" failures. Glass keeps everything (its local Python
REM     environment, settings, saved projects) right next to this launcher,
REM     so if THIS spot isn't writable, nothing downstream will be either -
REM     catch it here with a clear fix instead of a confusing failure later.
set "WRITETEST=%~dp0.glass_write_test"
(echo test> "%WRITETEST%") 2>nul
if not exist "%WRITETEST%" (
  color 0C
  echo   [X] This folder isn't writable:
  echo       %~dp0
  echo.
  echo       Glass needs to create a few files right next to this launcher -
  echo       a local Python environment, your settings, saved projects. Your
  echo       account can't write here, which usually means Glass was
  echo       extracted into Program Files or a company-managed folder.
  echo.
  echo       Fix: move the WHOLE extracted Glass folder somewhere you own,
  echo       like your Desktop or Documents ^(a normal drag-and-drop, no
  echo       admin rights needed^), then run this launcher again from there.
  echo.
  pause
  exit /b 1
)
del /q "%WRITETEST%" >nul 2>nul

REM --- one-time "what Glass does" disclosure - must type yes once ------------
REM     (delete core\.glass_insight_ok to see this again on the next launch)
set "INSIGHT=%~dp0core\.glass_insight_ok"
if exist "%INSIGHT%" goto insight_done

echo   Before first launch, here's a plain rundown of what Glass touches on
echo   your system. Nothing here is hidden - read it once, then type yes.
echo.
echo   RENDERING ENGINE
echo     - The web view itself is Chromium ^(the open-source engine behind
echo       Google Chrome^), via Qt WebEngine. There's no realistic way to
echo       browse the modern web without an engine like this one - but
echo       Glass's own behavior around it ^(tracking, storage, telemetry^)
echo       is separate, and is what the rest of this list covers.
echo.
echo   RUNS LOCALLY
echo     - Everything runs on this machine, in this folder. Glass creates a
echo       local Python environment ^(.venv^) here, and copies the interpreter
echo       to Glass.exe just so Task Manager shows "Glass" instead of "python".
echo.
echo   WHAT'S STORED, AND WHERE
echo     - Settings, browsing history, themes, and your projects are saved as
echo       plain files inside this same Glass folder. Nothing is sent
echo       anywhere else.
echo     - Saved logins go through a separate local vault, not a plain file:
echo       on Windows they're encrypted with your Windows account's own
echo       OS-level key ^(DPAPI^), unreadable on another account or machine.
echo       On Mac/Linux it's light obfuscation only, NOT real encryption -
echo       don't treat it as a hardened password manager either way.
echo.
echo   NETWORK ACCESS GLASS MAKES ON ITS OWN ^(not from your browsing^)
echo     - First run only: installs Python packages ^(PyQt6 etc^) from PyPI.
echo     - First run only: downloads the Widevine DRM component from Google
echo       ^(dl.google.com^), needed to play sites like Netflix/Crunchyroll.
echo       Skipped automatically if Chrome or Edge is already installed.
echo       Every fetch is logged with its exact URL and file hash in
echo       core\widevine_fetch_log.json - nothing about it is silent.
echo.
echo   BROWSING ITSELF
echo     - Glass is a browser: it fetches whatever pages you visit, same as
echo       any browser. Default search engine is DuckDuckGo, not Google. A
echo       local ad/tracker blocklist runs on-device; no telemetry is sent
echo       anywhere by Glass itself.
echo.
echo   DRM VIDEO FALLBACK
echo     - For DRM sites this engine can't decrypt itself, depending on your
echo       Settings choice, Glass may open a SEPARATE small Microsoft Edge
echo       ^(WebView2^) window in its own isolated profile - it can't see your
echo       real Edge data, and its telemetry/sync are turned off.
echo.
echo   No analytics, no crash reporting, no phone-home from Glass itself.
echo.
:insight_ask
set "AGREE="
set /p "AGREE=  Type yes to confirm you understand, and continue: "
if /i "%AGREE%"=="yes" goto insight_agreed
if /i "%AGREE%"=="no" (
  echo.
  echo   OK - not launching. Run this again whenever you're ready.
  pause
  exit /b 0
)
echo   ^(please type yes or no^)
goto insight_ask

:insight_agreed
echo Glass insight acknowledged %DATE% %TIME%> "%INSIGHT%"
echo.
:insight_done

where python >nul 2>nul
if errorlevel 1 (
  color 0C
  echo   [X] Python not found.
  echo.
  echo       Get it from python.org/downloads and click "Install Now" as-is -
  echo       that default already installs just for your account ^(into your
  echo       AppData folder^), no admin rights needed. Skip "Customize
  echo       installation" -^> "Install for all users" - that's the only part
  echo       of it that asks for admin. Tick "Add python.exe to PATH" on the
  echo       first screen, then run this launcher again.
  echo.
  pause
  exit /b 1
)

REM --- check for an update before anything else - if one changes           ----
REM     requirements.txt, the pip install step right after this picks it up
REM     automatically. Uses bare system python (updater.py is stdlib-only,
REM     no venv needed yet) and never blocks launch: offline, GitHub
REM     rate-limited, no releases yet, whatever - it just falls through.
python "%~dp0updater.py"

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo   [*] First run: creating a local environment...
  python -m venv "%~dp0.venv"
  if not exist "%~dp0.venv\Scripts\python.exe" (
    color 0E
    echo.
    echo   [!] Couldn't create a local Python environment in this folder, so
    echo       Glass will use your system-wide Python instead. If installing
    echo       dependencies below fails with a permissions error, that's
    echo       almost always because that system Python was installed "for
    echo       all users" ^(which needs admin^) - the step below already
    echo       retries without admin automatically if the first attempt fails.
    echo.
  )
)

set "USING_VENV=1"
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PY%" (
  set "PY=python"
  set "USING_VENV=0"
)
if not exist "%PYW%" set "PYW=%PY%"

REM --- run the app under the name "Glass" (so Task Manager / taskbar say Glass,
REM     not python). We copy the interpreter to Glass.exe and launch that. -------
set "GLASSEXE=%~dp0.venv\Scripts\Glass.exe"
if exist "%PYW%" (
  if not exist "%GLASSEXE%" copy /y "%PYW%" "%GLASSEXE%" >nul 2>nul
)
if not exist "%GLASSEXE%" set "GLASSEXE=%PYW%"

echo   [*] Checking dependencies (first run can take a minute)...
"%PY%" -m pip install --disable-pip-version-check -r "%~dp0requirements.txt"
if errorlevel 1 (
  if "%USING_VENV%"=="0" (
    echo   [*] That needs admin rights here - retrying without them...
    "%PY%" -m pip install --disable-pip-version-check --user -r "%~dp0requirements.txt"
  )
)
if errorlevel 1 (
  color 0E
  echo.
  echo   [!] Some dependencies may not have installed. Trying to start anyway...
)
echo.

echo   [*] Checking video DRM support (first run may download Widevine)...
"%PY%" "%~dp0core\widevine_setup.py"
echo.

set "READY=%~dp0core\.glass_ready"
if exist "%READY%" del /q "%READY%" >nul 2>nul

echo   [*] Starting Glass...
start "Glass" "%GLASSEXE%" "%~dp0core\launch.py"

set /a tries=0
:wait
if exist "%READY%" goto ready
set /a tries+=1
if %tries% GEQ 90 goto timeout
ping -n 2 127.0.0.1 >nul
<nul set /p "=."
goto wait

:ready
echo.
echo   [+] Glass is open. Closing this window...
ping -n 2 127.0.0.1 >nul
exit

:timeout
color 0E
echo.
echo   [!] Glass did not finish starting. It may still be loading, or a
echo       dependency failed. To see the actual error, run this in the folder:
echo.
echo          "%PY%" "%~dp0core\launch.py"
echo.
pause
exit /b 1
