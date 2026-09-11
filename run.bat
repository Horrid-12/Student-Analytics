@echo off
title GitHub Student Analytics Dashboard - Local Server

cd /d "%~dp0"

set PORT=8001

echo ===================================================
echo  Starting GitHub Student Analytics Dashboard...
echo  Opening browser at: http://localhost:%PORT%
echo ===================================================
echo.

set "PYTHON_CMD="

rem Prefer the project virtual environment only when its runtime is usable.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import uvicorn" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=.venv\Scripts\python.exe"
)

rem Fall back to the active machine Python when an older or empty .venv exists.
if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import uvicorn" >nul 2>nul
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] A Python runtime with uvicorn was not found.
    echo Install the dependencies with: python -m pip install -r requirements.txt
    exit /b 1
)

echo Using Python runtime: %PYTHON_CMD%
%PYTHON_CMD% -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --reload

echo.
echo ===================================================
echo  Server stopped.
echo ===================================================
pause
