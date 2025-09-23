@echo off
setlocal EnableExtensions EnableDelayedExpansion
color 0B
title Pocket Comfy Installer

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo.
echo =====================================================
echo   Pocket Comfy Installer
echo =====================================================
echo This will configure Pocket Comfy.
echo It can use a local venv or your system Python.
echo.
set /p _c=Press Enter to continue... 

:: -------- Python detection --------
set "PY_SYS="
for %%P in (py.exe) do if exist "%%~$PATH:P" set "PY_SYS=%%~$PATH:P"
if not defined PY_SYS for %%P in (python.exe) do if exist "%%~$PATH:P" set "PY_SYS=%%~$PATH:P"

if not defined PY_SYS (
  echo.
  echo Python not found on PATH.
  echo Install Python 3.10+ then re-run this installer.
  echo https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

:: Prefer 64-bit 3.10+; try explicit launchers first
set "PY_CALL="
for /f "usebackq tokens=*" %%V in (`where py 2^>NUL`) do set "HAS_PY=1"
if defined HAS_PY (
  py -3.12 -V >NUL 2>&1 && set "PY_CALL=py -3.12"
  if not defined PY_CALL py -3.11 -V >NUL 2>&1 && set "PY_CALL=py -3.11"
  if not defined PY_CALL py -3.10 -V >NUL 2>&1 && set "PY_CALL=py -3.10"
)
if not defined PY_CALL set "PY_CALL=%PY_SYS%"

echo Using Python: %PY_CALL%
for /f "tokens=2 delims= " %%v in ('%PY_CALL% -V') do set "PY_VER=%%v"
echo Detected version: %PY_VER%

:: -------- Collect paths --------
echo.
echo Enter absolute path to ComfyUI launcher (bat/sh). Example: C:\ComfyUI\run_nvidia_gpu.bat
set "COMFY_PATH="
set /p COMFY_PATH=ComfyUI path []: 

echo.
echo Enter absolute path to ComfyUI Mini start script. Leave blank if not used.
set "MINI_PATH="
set /p MINI_PATH=Mini path []: 

echo.
echo Enter absolute path to Smart Gallery start script. Leave blank if not used.
set "SMART_GALLERY_PATH="
set /p SMART_GALLERY_PATH=Smart Gallery path []: 

:: -------- Ports with defaults --------
set "COMFY_PORT="
set /p COMFY_PORT=ComfyUI port [8188]: 
if not defined COMFY_PORT set "COMFY_PORT=8188"

set "MINI_PORT="
set /p MINI_PORT=Mini port [3000]: 
if not defined MINI_PORT set "MINI_PORT=3000"

set "SMART_GALLERY_PORT="
set /p SMART_GALLERY_PORT=Smart Gallery port [7860]: 
if not defined SMART_GALLERY_PORT set "SMART_GALLERY_PORT=7860"

set "FLASK_PORT="
set /p FLASK_PORT=PocketComfy control port [5000]: 
if not defined FLASK_PORT set "FLASK_PORT=5000"

:: -------- Optional credentials --------
echo.
echo Optional: set a panel login password. Leave blank for no login.
set "LOGIN_PASS="
set /p LOGIN_PASS=Login password []: 

echo Optional: set a delete password for admin actions. Leave blank to disable delete endpoints.
set "DELETE_PASSWORD="
set /p DELETE_PASSWORD=Delete password []: 

echo Optional: target folder path for delete/recreate admin actions. Leave blank to disable.
set "DELETE_PATH="
set /p DELETE_PATH=Delete target path []: 

:: -------- LAN IP detection (best-effort) --------
for /f "usebackq tokens=*" %%I in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1'} | Select -First 1 -ExpandProperty IPAddress)"`) do set "LAN_IP=%%I"
if not defined LAN_IP set "LAN_IP=127.0.0.1"
echo Detected LAN IP: %LAN_IP%

:: -------- Create venv (optional) --------
set "VENV=%ROOT%venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "USE_VENV=1"
if not exist "%VENV%" (
  echo.
  echo Creating Python venv...
  %PY_CALL% -m venv "%VENV%"
  if errorlevel 1 (
    echo Failed to create venv. Will continue without a venv.
    set "USE_VENV="
  )
)

:: -------- Select Python executable --------
set "PY_EXE=%PY_CALL%"
if defined USE_VENV if exist "%VENV_PY%" set "PY_EXE=%VENV_PY%"

:: -------- Ensure pip and install deps --------
echo.
echo Installing Python dependencies...
if "%PY_EXE%"=="%VENV_PY%" (
  "%PY_EXE%" -m pip install --upgrade pip
  "%PY_EXE%" -m pip install -r "%ROOT%requirements.txt"
) else (
  %PY_EXE% -m pip install --upgrade pip --user
  %PY_EXE% -m pip install -r "%ROOT%requirements.txt" --user
)
if errorlevel 1 (
  echo.
  echo pip install reported an error. You can retry later with:
  echo   %PY_EXE% -m pip install -r "%ROOT%requirements.txt" [--user]
  echo Continuing setup.
)

:: -------- Write env file --------
(
  echo FLASK_PORT=%FLASK_PORT%
  echo COMFY_PORT=%COMFY_PORT%
  echo MINI_PORT=%MINI_PORT%
  echo SMART_GALLERY_PORT=%SMART_GALLERY_PORT%
  echo COMFY_PATH=%COMFY_PATH%
  echo MINI_PATH=%MINI_PATH%
  echo SMART_GALLERY_PATH=%SMART_GALLERY_PATH%
  echo LOGIN_PASS=%LOGIN_PASS%
  echo DELETE_PASSWORD=%DELETE_PASSWORD%
  echo DELETE_PATH=%DELETE_PATH%
)> "%ROOT%PocketComfy.env"

echo.
echo Install complete.
echo Launch with PocketComfy.bat
echo Control panel: http://%LAN_IP%:%FLASK_PORT%
echo.
pause
exit /b 0
