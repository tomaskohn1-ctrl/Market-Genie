@echo off
title Market Genie Server
color 0A

echo.
echo  ==========================================
echo   Market Genie - Starting Up...
echo  ==========================================
echo.

REM ── Find Python ────────────────────────────────────
echo [STEP 1] Locating Python...

REM Try 'py' launcher first (Windows Python Launcher - always works)
py --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=py
    goto :python_found
)

REM Try 'python' command
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON=python
    goto :python_found
)

REM Try common install paths directly
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
    set PYTHON="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    goto :python_found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set PYTHON="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    goto :python_found
)
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set PYTHON="%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    goto :python_found
)
if exist "C:\Python313\python.exe" (
    set PYTHON="C:\Python313\python.exe"
    goto :python_found
)
if exist "C:\Python312\python.exe" (
    set PYTHON="C:\Python312\python.exe"
    goto :python_found
)

REM Not found anywhere
echo.
echo  *** ERROR: Python not found! ***
echo.
echo  Please install Python:
echo  1. Go to https://python.org/downloads
echo  2. Click "Download Python"
echo  3. Run the installer
echo  4. CHECK THE BOX: "Add Python to PATH"  ^<-- VERY IMPORTANT
echo  5. Click "Install Now"
echo  6. Then run this file again.
echo.
pause
exit /b 1

:python_found
echo  Found Python: %PYTHON%
%PYTHON% --version
echo.

REM ── Install packages ───────────────────────────────
echo [STEP 2] Installing/upgrading required packages...
%PYTHON% -m pip install flask flask-cors requests "yfinance>=0.2.40" python-dotenv curl_cffi numpy --upgrade --quiet
if %errorlevel% neq 0 (
    echo.
    echo  *** pip install failed - trying with --user flag ***
    %PYTHON% -m pip install flask flask-cors requests "yfinance>=0.2.40" python-dotenv curl_cffi numpy --upgrade --quiet --user
    if %errorlevel% neq 0 (
        echo  *** ERROR: Could not install packages ***
        echo  Try right-clicking this file and selecting "Run as administrator"
        pause
        exit /b 1
    )
)
echo  Packages OK!
echo.

REM ── Launch server ──────────────────────────────────
echo [STEP 3] Launching Market Genie server...
echo  Dashboard will open at http://localhost:5000
echo  Keep this window open while using the dashboard.
echo  Press Ctrl+C to stop.
echo.

cd /d "C:\users\tomas\Documents\Claude\Projects\Market Genie"
%PYTHON% market_genie_server.py

echo.
echo  Server stopped.
pause
