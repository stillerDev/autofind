@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Fast native path: the exact web UI in a WebView2 window, no Python on the
rem launch path. If the shell is already resident, only raise its window.
set "SHELL=%~dp0shell-app\target\release\autofind-shell.exe"
if not exist "%SHELL%" goto no_shell

rem The app reads AUTOFIND_HOME; use the very same directory here or a second
rem double-click never sees the resident shell's pid and opens a second window.
set "HOME=%USERPROFILE%\.autofind"
if defined AUTOFIND_HOME set "HOME=%AUTOFIND_HOME%"

set "PYW="
for %%I in (pythonw.exe) do if exist "%%~$PATH:I" set "PYW=%%~$PATH:I"
if defined PYW set "PYTHON=%PYW%"

set "PIDF=%HOME%\shell.pid"
if not exist "%PIDF%" goto start_shell
set "SPID="
set /p SPID=<"%PIDF%"
if not defined SPID goto start_shell
tasklist /FI "PID eq %SPID%" /NH | findstr /I "autofind-shell.exe" >nul
if not errorlevel 1 (
  echo 1> "%HOME%\shell-show.flag"
  exit /b 0
)
rem Stale pid from a killed shell: without this check the launcher exits
rem silently and the user falls back to a browser tab.
del "%PIDF%" >nul 2>&1

:start_shell
start "" "%SHELL%"
exit /b 0

:no_shell
set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  echo.
  echo Python 3.11 или новее не найден в PATH.
  echo Установите его с python.org и запустите файл снова.
  echo.
  pause
  exit /b 1
)

%PY% -c "import numpy, PIL" >nul 2>nul
if errorlevel 1 (
  echo Устанавливаются зависимости: это займёт минуту при первом запуске...
  %PY% -m pip install -r requirements.txt
)

set "PYW="
for %%I in (pythonw.exe) do if exist "%%~$PATH:I" set "PYW=%%~$PATH:I"
if defined PYW (
  start "" "%PYW%" "%~dp0run.py" %*
  exit /b 0
)

%PY% run.py %*
if errorlevel 1 pause
