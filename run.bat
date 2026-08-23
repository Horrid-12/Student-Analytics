@echo off
title GitHub Student Analytics Dashboard - Local Server

cd /d "%~dp0"

echo ===================================================
echo  Starting GitHub Student Analytics Dashboard...
echo  Opening browser at: http://localhost:8501
echo ===================================================
echo.

where streamlit >nul 2>nul
if %ERRORLEVEL% equ 0 (
    streamlit run app.py --server.headless false --server.port 8501
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        python -m streamlit run app.py --server.headless false --server.port 8501
    ) else (
        where py >nul 2>nul
        if %ERRORLEVEL% equ 0 (
            py -m streamlit run app.py --server.headless false --server.port 8501
        ) else (
            echo [ERROR] Neither Streamlit nor Python was found in your PATH.
            echo Please ensure Python and Streamlit are installed.
        )
    )
)

echo.
echo ===================================================
echo  Server stopped.
echo ===================================================
pause
