@echo off
REM ────────────────────────────────────────────────────────────────────
REM Tigress Bond Pricing — daily launcher.
REM Started silently by TigressBondPricing.vbs (no console window visible).
REM
REM What it does:
REM   1. cd to the install folder (where this .bat lives)
REM   2. find a working Python interpreter (py.exe launcher OR absolute path)
REM   3. start server.py in a background cmd that redirects all output
REM      to server.log next to this file
REM   4. wait 3 seconds for the HTTP server to come up on port 5050
REM   5. open the default browser to http://localhost:5050/
REM
REM Bloomberg Terminal MUST be open and logged in before clicking the
REM desktop icon. server.py talks to localhost:8194 via blpapi and will
REM fail to connect if Terminal isn't running.
REM ────────────────────────────────────────────────────────────────────
setlocal

REM cd to wherever this .bat lives (the install folder)
cd /d "%~dp0"

REM ── Locate a working Python interpreter ──
REM Try the py launcher first (default with Python 3 installer), then
REM fall back to common install locations. PY_EXE ends up holding the
REM working path. If nothing is found, write an error to server.log
REM and exit.
set "PY_EXE="

REM Try explicit install paths FIRST -- the 'py' launcher on PATH can
REM be intercepted by the Microsoft Store stub which doesn't actually
REM run Python.
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :found_py
)

if exist "%ProgramFiles%\Python312\python.exe" (
    set "PY_EXE=%ProgramFiles%\Python312\python.exe"
    goto :found_py
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :found_py
)

if exist "C:\Python312\python.exe" (
    set "PY_EXE=C:\Python312\python.exe"
    goto :found_py
)

REM Last resort: py launcher (may hit Store stub on some machines)
where py >NUL 2>&1
if not errorlevel 1 (
    set "PY_EXE=py -3"
    goto :found_py
)

REM No Python found
echo Python 3.12 not found. Run setup-boss-machine.bat first. > server.log
exit /b 1

:found_py

REM ── Start server.py in a hidden background cmd that captures output ──
REM We wrap in `cmd /c` so the > redirect is interpreted by THAT cmd,
REM not the outer one — otherwise the redirect file is empty.
REM `start /B` keeps things in the same console (no new window), which
REM combined with the .vbs window-state-0 means nothing flashes.
start /B "" cmd /c "%PY_EXE% server.py > server.log 2>&1"

REM ── Wait for the server to bind port 5050 ──
REM Poll up to 10 times (5 seconds total) — server.py takes ~1-2s to
REM connect to Bloomberg + Mongo on cold start.
set /a tries=0
:wait_loop
set /a tries+=1
timeout /t 1 /nobreak >NUL 2>&1
REM Use PowerShell's Test-NetConnection-like check via curl if available,
REM else just sleep through. Most boss machines won't have curl on PATH,
REM so we just sleep 3 seconds total via the timeout loop and trust it.
if %tries% lss 3 goto :wait_loop

REM ── Open the user's default browser to the login screen ──
start "" "http://localhost:5050/"

endlocal
