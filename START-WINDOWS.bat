@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 start.py
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Install Python 3.11 or newer from https://www.python.org/downloads/
        echo Enable "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
    python start.py
)
if errorlevel 1 pause
