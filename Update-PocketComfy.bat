@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Update Pocket Comfy

rem === Normalize working dir ===
cd /d "%~dp0"
set "DEST=%CD%"
if not "%DEST:~-2%"==":\" if "%DEST:~-1%"=="\" set "DEST=%DEST:~0,-1%"
set "LOG=%DEST%\update_log.txt"
echo [%DATE% %TIME%] start>"%LOG%"
echo Updating in: "%DEST%"

rem === Preserve user config ===
if exist "%DEST%\PocketComfy.env" copy /Y "%DEST%\PocketComfy.env" "%DEST%\PocketComfy.env.bak" >NUL

rem === Try git fast-path ===
where git >NUL 2>&1
if not errorlevel 1 (
  if exist "%DEST%\.git\" (
    echo Git repo detected. Fetching...>>"%LOG%"
  ) else (
    echo Initializing git repo...>>"%LOG%"
    git -C "%DEST%" init || goto :download
    git -C "%DEST%" remote add origin https://github.com/PastLifeDreamer/Pocket-Comfy.git 2>NUL
    rem Exclude local-only files from tracking
    mkdir "%DEST%\.git\info" >NUL 2>&1
    (
      echo PocketComfy.env
      echo PocketComfy.env.bak
      echo update_log.txt
      echo install_log.txt
      echo venv/
      echo __pycache__/
    )>>"%DEST%\.git\info\exclude"
  )
  git -C "%DEST%" fetch --depth=1 origin main 2>NUL || git -C "%DEST%" fetch --depth=1 origin master || goto :download
  git -C "%DEST%" rev-parse --verify --quiet origin/main >NUL 2>&1 && (
    git -C "%DEST%" checkout -f -B main origin/main && goto :done
  )
  git -C "%DEST%" rev-parse --verify --quiet origin/master >NUL 2>&1 && (
    git -C "%DEST%" checkout -f -B master origin/master && goto :done
  )
)

rem === Fallback: download ZIP and copy ===
:download
set "TMP=%TEMP%\PC_Update_%RANDOM%%RANDOM%"
set "ZIP=%TMP%\repo.zip"
set "ZIP_MAIN=https://codeload.github.com/PastLifeDreamer/Pocket-Comfy/zip/refs/heads/main"
set "ZIP_MASTER=https://codeload.github.com/PastLifeDreamer/Pocket-Comfy/zip/refs/heads/master"
mkdir "%TMP%" >NUL 2>&1

where curl >NUL 2>&1
if not errorlevel 1 (
  echo Downloading (curl) main...>>"%LOG%"
  curl.exe -fL --retry 3 --retry-delay 2 -o "%ZIP%" "%ZIP_MAIN%"
  if errorlevel 1 (
    echo curl main failed. Trying master...>>"%LOG%"
    curl.exe -fL --retry 3 --retry-delay 2 -o "%ZIP%" "%ZIP_MASTER%"
  )
) else (
  echo Downloading (PowerShell) main...>>"%LOG%"
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::Expect100Continue=$false; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Try { Invoke-WebRequest -Uri '%ZIP_MAIN%' -OutFile '%ZIP%' -UseBasicParsing } Catch { exit 1 }"
  if errorlevel 1 (
    echo PS main failed. Trying master...>>"%LOG%"
    powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::Expect100Continue=$false; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Try { Invoke-WebRequest -Uri '%ZIP_MASTER%' -OutFile '%ZIP%' -UseBasicParsing } Catch { exit 1 }"
  )
)

for %%F in ("%ZIP%") do set "SIZE=%%~zF"
if not defined SIZE set "SIZE=0"
echo Zip size: %SIZE%>>"%LOG%"
if "%SIZE%"=="0" (
  echo Download failed. See update_log.txt
  goto :end
)

rem Use tar to extract (built-in on Win10+). Fallback to PowerShell Expand-Archive.
tar -xf "%ZIP%" -C "%TMP%" 2>NUL
if errorlevel 1 powershell -NoProfile -Command "Expand-Archive -Path '%ZIP%' -DestinationPath '%TMP%' -Force"

set "EXTDIR="
for /d %%D in ("%TMP%\Pocket-Comfy-*") do set "EXTDIR=%%~fD"
if not defined EXTDIR (
  echo Extracted folder not found. See update_log.txt
  goto :cleanup
)

echo Copying files...>>"%LOG%"
rem Robocopy: success is any code < 8
robocopy "%EXTDIR%" "%DEST%" /E /COPY:DAT /DCOPY:DA /R:1 /W:1 /NFL /NDL /NP /NJH /NJS /XO /XJ ^
 /XF PocketComfy.env PocketComfy.env.bak update_log.txt install_log.txt UpdatePocketComfy*.bat ^
 /XD venv .git deps __pycache__ >"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
if %RC% GEQ 8 (
  echo File copy failed. Code %RC%. See update_log.txt
  goto :cleanup
)

:done
echo [%DATE% %TIME%] done>>"%LOG%"
echo Update complete. Config and venv preserved.
goto :end

:cleanup
rd /s /q "%TMP%" >NUL 2>&1
goto :end

:end
pause
exit /b 0
