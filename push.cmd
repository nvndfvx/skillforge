@echo off
REM One-click push. Double-click this file.
REM ASCII only on purpose: cmd.exe reads .bat/.cmd as the OEM code page,
REM so the repo path is derived from %~dp0 instead of being hard-coded.

cd /d "%~dp0"
echo.
echo === SkillForge: git push ===
echo repo : %CD%
echo.

git push origin main

if errorlevel 1 (
    echo.
    echo === PUSH FAILED ===
    echo.
    echo  1. Make sure your proxy client is running (Clash / V2Ray ...).
    echo  2. If it still fails, check the port: git config --get http.proxy
    echo  3. Copy the error text above and send it back.
) else (
    echo.
    echo === PUSH OK ===
    echo.
    echo  CI: https://github.com/nvndfvx/skillforge/actions
)

echo.
pause
