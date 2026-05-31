@echo off
setlocal enabledelayedexpansion
title PDF Splitter v1.0

echo.
echo  ============================================================
echo   PDF Splitter v1.0  --  Portable Edition
echo  ============================================================
echo.

:: ── Check Python ─────────────────────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  Python not found on this PC.
    echo.
    echo  Attempting to install Python 3.12 via Windows Package Manager...
    echo  (This requires an internet connection and may take a minute.)
    echo.
    winget install Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    if %errorlevel% neq 0 (
        echo.
        echo  *** Auto-install failed. ***
        echo.
        echo  Please download Python manually from:
        echo    https://www.python.org/downloads/
        echo.
        echo  IMPORTANT: Tick "Add Python to PATH" during installation.
        echo.
        start https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo.
    echo  Python installed successfully.
    echo.
    :: Refresh PATH so python is available in this session
    call refreshenv >nul 2>&1
)

:: Confirm Python is now available
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  Python still not found after install attempt.
    echo  Please restart this launcher, or install Python manually.
    pause
    exit /b 1
)

:: ── Install required packages ─────────────────────────────────────────────────
echo  Checking required packages...
python -m pip install --quiet --upgrade pip >nul 2>&1
python -m pip install --quiet pypdf anthropic tkinterdnd2
if %errorlevel% neq 0 (
    echo.
    echo  *** Failed to install required packages. ***
    echo  Check your internet connection and try again.
    pause
    exit /b 1
)
echo  Packages OK.
echo.

:: ── Launch app ────────────────────────────────────────────────────────────────
echo  Starting PDF Splitter...
echo.
python "%~dp0pdf_splitter.py"

if %errorlevel% neq 0 (
    echo.
    echo  *** The application exited with an error. ***
    echo  Check the output above for details.
    pause
)
endlocal
