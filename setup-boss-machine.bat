@echo off
REM ====================================================================
REM Tigress Bond Pricing -- one-time boss machine setup.
REM
REM Run this ONCE per boss machine after extracting the BONDPRICING
REM folder from the email/zip. Safe to re-run if anything goes wrong.
REM
REM What it does:
REM   1. Locates or installs Python 3.12
REM   2. pip-installs pymongo (Mongo driver) using explicit python.exe path
REM   3. pip-installs blpapi from Bloomberg's official Python repo
REM   4. Creates a "Tigress Bond Pricing" shortcut on the desktop that
REM      points to TigressBondPricing.vbs in this folder.
REM
REM Prerequisites (boss should have these BEFORE running):
REM   - Bloomberg Terminal installed and logged in at least once
REM   - Internet access (for Python + pip downloads)
REM   - A .env file present in this folder with the Tigress secrets
REM     (your contact provides this -- do not share it).
REM ====================================================================
setlocal
cd /d "%~dp0"

echo.
echo === Tigress Bond Pricing Setup ===
echo.

REM -- Step 1: locate Python 3.12 (try explicit paths first; PATH is
REM    unreliable on Windows because the Microsoft Store stub for 'py'
REM    can intercept) --
set "PY_EXE="

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto HAVE_PYTHON
)
if exist "%ProgramFiles%\Python312\python.exe" (
    set "PY_EXE=%ProgramFiles%\Python312\python.exe"
    goto HAVE_PYTHON
)
if exist "C:\Python312\python.exe" (
    set "PY_EXE=C:\Python312\python.exe"
    goto HAVE_PYTHON
)

REM Not found in known locations -- install it
echo Python 3.12 not found. Downloading installer...
set "PYINST=%TEMP%\python-3.12.7-amd64.exe"
curl -sSL -o "%PYINST%" https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
if errorlevel 1 goto PY_DL_FAIL
echo Installing Python 3.12 silently (about 60 seconds)...
"%PYINST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
del "%PYINST%"

REM After install, Python lands at the per-user path
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    echo Python 3.12 installed.
    goto HAVE_PYTHON
)

echo ERROR: Python installer ran but python.exe not found at the expected
echo        location: %%LOCALAPPDATA%%\Programs\Python\Python312\python.exe
pause
exit /b 1

:PY_DL_FAIL
echo ERROR: Could not download Python installer. Check your internet connection.
pause
exit /b 1

:HAVE_PYTHON
echo Using Python: %PY_EXE%
"%PY_EXE%" --version
echo.

REM -- Step 2: pip packages (call python.exe directly so we don't depend
REM    on 'py' or PATH being refreshed mid-script) --
echo Upgrading pip...
"%PY_EXE%" -m pip install --upgrade pip --quiet

echo Installing pymongo...
"%PY_EXE%" -m pip install --quiet pymongo
if errorlevel 1 goto PYMONGO_FAIL

echo Installing blpapi (Bloomberg's official Python library)...
"%PY_EXE%" -m pip install --quiet --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
if errorlevel 1 goto BLPAPI_FAIL
goto AFTER_PIP

:PYMONGO_FAIL
echo ERROR: pymongo install failed.
pause
exit /b 1

:BLPAPI_FAIL
echo ERROR: blpapi install failed. Make sure Bloomberg Terminal is installed.
pause
exit /b 1

:AFTER_PIP

REM -- Step 3: Desktop shortcut --
echo.
echo Creating desktop shortcut...
set "SHORTCUT=%USERPROFILE%\Desktop\Tigress Bond Pricing.lnk"
set "TARGET=%~dp0TigressBondPricing.vbs"
set "WORKDIR=%~dp0"

powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath = '%TARGET%'; $s.WorkingDirectory = '%WORKDIR%'; $s.IconLocation = '%SystemRoot%\System32\imageres.dll,3'; $s.Description = 'Tigress Bond Pricing Engine'; $s.Save()"

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
