@echo off
title GitHub Student Analytics Dashboard - Local Server

cd /d "%~dp0"

set PORT=8001

echo ===================================================
echo  Starting GitHub Student Analytics Dashboard...
echo  Opening browser at: http://localhost:%PORT%
echo ===================================================
echo.

if exist ".venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe -m uvicorn app.main:app --port %PORT%
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        python -m uvicorn app.main:app --port %PORT%
    ) else (
        where py >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            py -m uvicorn app.main:app --port %PORT%
        ) else (
            echo [ERROR] Python was not found in your PATH.
            echo Please install Python and the dependencies in requirements.txt.
        )
    )
)

echo.
echo ===================================================
echo  Server stopped.
echo ===================================================
pause
