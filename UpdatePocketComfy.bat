@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Update Pocket Comfy

set "ROOT=%~dp0"
set "LOG=%ROOT%update_log.txt"
set "OWNER=PastLifeDreamer"
set "REPO=Pocket-Comfy"
set "TMP=%TEMP%\PC_Update_%RANDOM%%RANDOM%"
set "ZIP_MAIN=https://codeload.github.com/%OWNER%/%REPO%/zip/refs/heads/main"
set "ZIP_MASTER=https://codeload.github.com/%OWNER%/%REPO%/zip/refs/heads/master"

echo [%%DATE%% %%TIME%%] === Update start ===>"%LOG%"
echo Updating Pocket Comfy in "%ROOT%"
echo Log: %LOG%

:: --- Backup user config ---
if exist "%ROOT%PocketComfy.env" (
  copy /Y "%ROOT%PocketComfy.env" "%ROOT%PocketComfy.env.bak" >NUL
  echo Backed up PocketComfy.env to PocketComfy.env.bak>>"%LOG%"
)

:: --- Prefer git if repo ---
if exist "%ROOT%.git\" (
  where git >NUL 2>&1
  if not errorlevel 1 (
    echo Git repo detected. Pulling latest...
    pushd "%ROOT%"
    git pull --rebase --autostash
    set "GITERR=!ERRORLEVEL!"
    popd
    echo git pull exit !GITERR!>>"%LOG%"
    if "!GITERR!"=="0" goto postcopy
    echo git pull failed. Falling back to zip update...
  )
)

:: --- Download archive ---
mkdir "%TMP%" >NUL 2>&1
echo Downloading latest source (main)...
powershell -NoProfile -Command "Try { Invoke-WebRequest -Uri '%ZIP_MAIN%' -OutFile '%TMP%\repo.zip' -UseBasicParsing } Catch { exit 1 }"
if errorlevel 1 (
  echo Main branch download failed. Trying master...
  powershell -NoProfile -Command "Try { Invoke-WebRequest -Uri '%ZIP_MASTER%' -OutFile '%TMP%\repo.zip' -UseBasicParsing } Catch { exit 1 }"
  if errorlevel 1 (
    echo Download failed. See log.
    echo Download failed from both main and master.>>"%LOG%"
    pause
    exit /b 1
  )
)

echo Expanding archive...
powershell -NoProfile -Command "Expand-Archive -Path '%TMP%\repo.zip' -DestinationPath '%TMP%' -Force"

:: Find extracted folder (Pocket-Comfy-main or Pocket-Comfy-master)
set "EXTDIR="
for /d %%D in ("%TMP%\%REPO%*") do (
  set "EXTDIR=%%~fD"
  goto found
)
:found
if not defined EXTDIR (
  echo Could not locate extracted contents. See log.
  echo EXTDIR not found under %TMP%.>>"%LOG%"
  pause
  exit /b 1
)

echo Source: "%EXTDIR%" >>"%LOG%"
echo Dest  : "%ROOT%"    >>"%LOG%"
echo Copying files...

:: Single-line ROBOCOPY with clean quoting. Exclude user config and local env.
robocopy "%EXTDIR%" "%ROOT%" *.* /E /NFL /NDL /NP /NJH /NJS /R:1 /W:1 /XO ^
 /XF "PocketComfy.env" "PocketComfy.env.bak" "update_log.txt" "install_log.txt" "UpdatePocketComfy.bat" ^
 /XD "venv" ".git" "deps" "__pycache__" >>"%LOG%" 2>&1

set "RC=!ERRORLEVEL!"
:: Robocopy returns 0..7 for success
if !RC! GEQ 8 (
  echo File copy failed. Robocopy code !RC!. See log.
  echo Robocopy error !RC!.>>"%LOG%"
  pause
  exit /b !RC!
)

:: Cleanup temp
rd /s /q "%TMP%" >NUL 2>&1

:postcopy
echo [%DATE% %TIME%] === Update done ===>>"%LOG%"
echo Update complete. Your PocketComfy.env was preserved.

:: Optional: reinstall deps
set "PY_EXE=%ROOT%venv\Scripts\python.exe"
if not exist "%PY_EXE%" set "PY_EXE=py -3"

echo.
echo Reinstall dependencies now? [Y/N] (default N):
set /p __ans=
if /I "!__ans!"=="Y" (
  if exist "%ROOT%requirements.txt" (
    echo Upgrading pip and installing requirements...
    if exist "%ROOT%venv\Scripts\python.exe" (
      "%ROOT%venv\Scripts\python.exe" -m pip install --upgrade pip
      "%ROOT%venv\Scripts\python.exe" -m pip install -r "%ROOT%requirements.txt"
    ) else (
      %PY_EXE% -m pip install --upgrade pip --user
      %PY_EXE% -m pip install -r "%ROOT%requirements.txt" --user
    )
  )
)

echo.
echo Done.
pause
exit /b 0 
