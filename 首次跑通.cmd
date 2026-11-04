@echo off
REM Double-click this file. ASCII only (cmd.exe reads .cmd as the OEM code page).
REM It never asks you to type commands: every step is done by the script itself.
setlocal
cd /d "%~dp0"
echo.
echo ============================================================
echo  SkillForge first run  (double-click launcher)
echo ============================================================
echo.

python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found on PATH.
    echo.
    echo   Expected: D:\Python312\python.exe
    echo   Fix: open a NEW terminal and run  python --version
    echo        if that fails, re-open Settings -^> App execution aliases
    echo        and turn OFF python.exe / python3.exe
    echo.
    pause
    exit /b 1
)

python scripts\first_run.py
set "RC=%errorlevel%"

echo.
if not "%RC%"=="0" (
    echo ============================================================
    echo  Stopped with exit code %RC%
    echo ============================================================
    echo  Screenshot EVERYTHING from the step header down, and send it.
    echo.
)

echo.
pause
exit /b %RC%
