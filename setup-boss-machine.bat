@echo off
REM ────────────────────────────────────────────────────────────────────
REM Tigress Bond Pricing — one-time boss machine setup.
REM
REM Run this ONCE per boss machine after extracting the BONDPRICING
REM folder from the email/zip. Safe to re-run if anything goes wrong.
REM
REM What it does:
REM   1. Checks for Python 3.12; downloads + silently installs if missing
REM   2. pip-installs pymongo (Mongo driver)
REM   3. pip-installs blpapi from Bloomberg's official Python repo
REM   4. Creates a "Tigress Bond Pricing" shortcut on the desktop that
REM      points to TigressBondPricing.vbs in this folder.
REM
REM Prerequisites (boss should have these BEFORE running):
REM   - Bloomberg Terminal installed and logged in at least once
REM   - Internet access (for Python + pip downloads)
REM   - A .env file present in this folder with the Tigress secrets
REM     (your contact provides this — do not share it).
REM ────────────────────────────────────────────────────────────────────
setlocal
cd /d "%~dp0"

echo.
echo === Tigress Bond Pricing Setup ===
echo.

REM ── Step 1: Python 3.12 ──
py -3.12 --version >NUL 2>&1
if errorlevel 1 (
    echo Python 3.12 not found. Downloading installer...
    set "PYINST=%TEMP%\python-3.12.7-amd64.exe"
    curl -sSL -o "%PYINST%" https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
    if errorlevel 1 (
        echo ERROR: Could not download Python installer. Check your internet connection.
        pause
        exit /b 1
    )
    echo Installing Python 3.12 silently (this takes ~60 seconds)...
    "%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
    del "%PYINST%"
    echo Python 3.12 installed.
) else (
    echo Python 3.12 already installed.
)

REM ── Step 2: pip packages ──
echo.
echo Upgrading pip...
py -3.12 -m pip install --upgrade pip --quiet

echo Installing pymongo...
py -3.12 -m pip install --quiet pymongo
if errorlevel 1 (
    echo ERROR: pymongo install failed.
    pause
    exit /b 1
)

echo Installing blpapi (Bloomberg's official Python library)...
py -3.12 -m pip install --quiet --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
if errorlevel 1 (
    echo ERROR: blpapi install failed. Make sure Bloomberg Terminal is installed.
    pause
    exit /b 1
)

REM ── Step 3: Desktop shortcut ──
echo.
echo Creating desktop shortcut...
set "SHORTCUT=%USERPROFILE%\Desktop\Tigress Bond Pricing.lnk"
set "TARGET=%~dp0TigressBondPricing.vbs"

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath = '%TARGET%';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.IconLocation = '%SystemRoot%\System32\imageres.dll,3';" ^
  "$s.Description = 'Tigress Bond Pricing Engine';" ^
  "$s.Save()"

echo.
echo === Setup Complete ===
echo.
echo Look for "Tigress Bond Pricing" on your desktop.
echo Double-click it to start. Your browser will open to the login screen.
echo.
echo Sign in with the email + password you were given by your Tigress contact.
echo.
pause
endlocal
