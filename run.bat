@echo off
cd /d "%~dp0"

echo ============================================
echo   NEM Battery SoC Tracker - startup
echo ============================================

REM --- 1. Check if Python is installed ---
python --version >nul 2>&1
if %errorlevel%==0 (
    echo Python found. Skipping install.
    goto :venv
)

echo Python not found. Attempting install via winget...

REM --- 2. Try winget (built into Windows 10/11) ---
winget --version >nul 2>&1
if %errorlevel%==0 (
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
) else (
    echo.
    echo ERROR: winget is not available on this machine.
    echo Please install Python 3.9+ manually from https://www.python.org/downloads/
    echo IMPORTANT: tick "Add Python to PATH" during installation.
    echo Then re-run this file.
    echo.
    pause
    exit /b 1
)

REM --- 3. Refresh PATH for this session and re-check ---
echo Refreshing environment...
for /f "skip=2 tokens=2,*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "syspath=%%b"
for /f "skip=2 tokens=2,*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "userpath=%%b"
set "PATH=%syspath%;%userpath%"

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo Python was installed but is not yet visible on PATH.
    echo Please CLOSE this window and double-click run.bat again.
    echo.
    pause
    exit /b 1
)
echo Python installed successfully.

:venv
REM --- 4. Create virtual environment if missing ---
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

REM --- 5. Install dependencies ---
echo Installing dependencies...
pip install -q -r requirements.txt

echo.
echo Starting NEM Battery Tracker at http://127.0.0.1:8051
python app.py
