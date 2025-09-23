@echo off
setlocal EnableExtensions EnableDelayedExpansion

title Pocket Comfy
set "ROOT=%~dp0"
set "SCRIPT=%ROOT%PocketComfy.py"

:: Prefer local venv if present
set "VENV_PY=%ROOT%venv\Scripts\python.exe"
set "PYEXE="
if exist "%VENV_PY%" set "PYEXE=%VENV_PY%"
if not defined PYEXE for %%P in (py.exe) do if exist "%%~$PATH:P" set "PYEXE=py -3"
if not defined PYEXE for %%P in (python.exe) do if exist "%%~$PATH:P" set "PYEXE=%%~$PATH:P"

if not defined PYEXE (
  echo Python not found. Install Python 3.10+ and rerun.
  pause
  exit /b 1
)

echo Launching Pocket Comfy...
start "Pocket Comfy" /D "%ROOT%" %PYEXE% -u "%SCRIPT%"
exit /b 0
