@echo off
REM Double-click launcher for recording a REAL system. ASCII only.
REM Ask for the URL once, then run: login -> record v1 -> record v2 -> summary.
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo ============================================================
    echo  Record a REAL system  (login + two demos)
    echo ============================================================
    echo.
    set /p URL=Page URL to record: 
    set /p SID=Skill id (e.g. vamaudit): 
) else (
    set "URL=%~1"
    set "SID=%~2"
)

if "%URL%"=="" (
    echo [ERROR] URL is required.
    pause
    exit /b 1
)
if "%SID%"=="" set "SID=realjob"

python -c "import sys" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] python not found. Open a NEW terminal and run: python --version
    pause
    exit /b 1
)

python scripts\real_record.py --url "%URL%" --skill-id "%SID%"
set "RC=%errorlevel%"

echo.
echo ============================================================
if not "%RC%"=="0" (
    echo  Stopped with exit code %RC%
) else (
    echo  Done. Send the summary above to the assistant.
)
echo ============================================================
echo.
pause
exit /b %RC%
