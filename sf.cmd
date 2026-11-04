@echo off
REM SkillForge launcher. ASCII only: cmd.exe reads .cmd with the OEM code page.
REM
REM Why this file exists: on machines where `python` is only the Microsoft Store
REM app-execution-alias stub (0-byte exe in WindowsApps), `python -m skillforge`
REM prints "Python was not found" instead of running. This finds a real Python.
REM
REM Order:
REM   1. %SKILLFORGE_PYTHON%  (set this to pin a specific interpreter)
REM   2. py -3                (official Windows launcher)
REM   3. python               (only if it actually runs, i.e. not the stub)
REM   4. DSH bundled python   (fallback for this machine)
setlocal
set "REPO=%~dp0"
set "PY="

if defined SKILLFORGE_PYTHON if exist "%SKILLFORGE_PYTHON%" set "PY=%SKILLFORGE_PYTHON%"

if not defined PY (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)

if not defined PY (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  if exist "D:\dsh-home\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" (
    set "PY=D:\dsh-home\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
  )
)

if not defined PY (
  echo [sf] No real Python found.
  echo      Install one:  winget install Python.Python.3.12
  echo      Or pin it:    set SKILLFORGE_PYTHON=C:\path\to\python.exe
  exit /b 1
)

set "PYTHONPATH=%REPO%vendor"
REM Do NOT set PYTHONIOENCODING=utf-8 here: a Chinese console runs on cp936,
REM and forcing UTF-8 output makes it mojibake. Let Python follow the console.
cd /d "%REPO%"
%PY% -m skillforge %*
exit /b %errorlevel%
