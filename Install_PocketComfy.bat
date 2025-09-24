@echo off
setlocal EnableExtensions EnableDelayedExpansion
color 0B
title Pocket Comfy Installer

set "ROOT=%~dp0"
cd /d "%ROOT%"
set "LOG=%ROOT%install_log.txt"

echo. > "%LOG%"
call :log "=== Pocket Comfy Installer start ==="

echo.
echo =====================================================
echo   Pocket Comfy Installer
echo =====================================================
echo This will configure Pocket Comfy and install Python deps.
echo It can use a local venv or your system Python.
echo.
set /p _c=Press Enter to continue... 

:: ---------------- PYTHON DETECTION ----------------
set "PY_CALL="
where py >NUL 2>&1 && (py -3.12 -V >NUL 2>&1 && set "PY_CALL=py -3.12")
if not defined PY_CALL where py >NUL 2>&1 && (py -3.11 -V >NUL 2>&1 && set "PY_CALL=py -3.11")
if not defined PY_CALL where py >NUL 2>&1 && (py -3.10 -V >NUL 2>&1 && set "PY_CALL=py -3.10")

if not defined PY_CALL (
  for %%P in (python.exe) do if exist "%%~$PATH:P" set "PY_CALL=%%~$PATH:P"
)

if not defined PY_CALL (
  echo Python not found on PATH. Install Python 3.10+ then re-run.
  call :log "ERROR: Python not found"
  pause
  exit /b 1
)
for /f "tokens=2 delims= " %%v in ('%PY_CALL% -V') do set "PY_VER=%%v"
echo Using Python: %PY_CALL%  (version %PY_VER%)
call :log "Using Python: %PY_CALL% (%PY_VER%)"

:: ---------------- USER INPUT ----------------
echo.
echo Enter absolute path to ComfyUI launcher (bat/sh). Example: C:\ComfyUI\run_nvidia_gpu.bat
set "COMFY_PATH="
set /p COMFY_PATH=ComfyUI path []: 
set "COMFY_PATH=!COMFY_PATH:\"=!"

echo.
echo Enter absolute path to ComfyUI Mini start script. Leave blank if not used.
set "MINI_PATH="
set /p MINI_PATH=Mini path []: 
set "MINI_PATH=!MINI_PATH:\"=!"

echo.
echo Enter absolute path to Smart Gallery start script (smartgallery.py). Leave blank if not used.
set "SMART_GALLERY_PATH="
set /p SMART_GALLERY_PATH=Smart Gallery path []: 
set "SMART_GALLERY_PATH=!SMART_GALLERY_PATH:\"=!"

set "COMFY_PORT="
set /p COMFY_PORT=ComfyUI port [8188]: 
if not defined COMFY_PORT set "COMFY_PORT=8188"

set "MINI_PORT="
set /p MINI_PORT=Mini port [3000]: 
if not defined MINI_PORT set "MINI_PORT=3000"

set "SMART_GALLERY_PORT="
set /p SMART_GALLERY_PORT=Smart Gallery port [8189]: 
if not defined SMART_GALLERY_PORT set "SMART_GALLERY_PORT=8189"

set "FLASK_PORT="
set /p FLASK_PORT=PocketComfy control port [5000]: 
if not defined FLASK_PORT set "FLASK_PORT=5000"

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
set "DELETE_PATH=!DELETE_PATH:\"=!"

:: ---------------- NETWORK INFO ----------------
for /f "usebackq tokens=*" %%I in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1'} | Select -First 1 -ExpandProperty IPAddress)"`) do set "LAN_IP=%%I"
if not defined LAN_IP set "LAN_IP=127.0.0.1"
echo Detected LAN IP: %LAN_IP%
call :log "LAN IP: %LAN_IP%"

:: ---------------- VENV (OPTIONAL) ----------------
set "VENV=%ROOT%venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "USE_VENV=1"
if not exist "%VENV%" (
  echo.
  echo Creating Python venv...
  call :log "Creating venv"
  %PY_CALL% -m venv "%VENV%"
  if errorlevel 1 (
    echo Failed to create venv. Will continue without a venv.
    call :log "WARN: venv creation failed"
    set "USE_VENV="
  )
)

set "PY_EXE=%PY_CALL%"
if defined USE_VENV if exist "%VENV_PY%" set "PY_EXE=%VENV_PY%"
echo Using interpreter for installs: %PY_EXE%
call :log "Installer Python: %PY_EXE%"

:: ---------------- PIP & REQUIREMENTS ----------------
echo.
echo Upgrading pip...
call :log "Upgrading pip"
if "%PY_EXE%"=="%VENV_PY%" (
  "%PY_EXE%" -m pip install --upgrade pip
) else (
  "%PY_EXE%" -m pip install --upgrade pip --user
)

echo Installing Pocket Comfy requirements...
call :log "Installing requirements.txt"
if exist "%ROOT%requirements.txt" (
  if "%PY_EXE%"=="%VENV_PY%" (
    "%PY_EXE%" -m pip install -r "%ROOT%requirements.txt"
  ) else (
    "%PY_EXE%" -m pip install -r "%ROOT%requirements.txt" --user
  )
) else (
  echo requirements.txt not found. Skipping pip install.
  call :log "WARN: requirements.txt missing"
)

:: ---------------- WRITE ENV ----------------
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
echo Log: %LOG%
echo.
pause
exit /b 0

:log
>> "%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0
