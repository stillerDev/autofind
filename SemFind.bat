@echo off
chcp 65001 >nul
cd /d "%~dp0"

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
  echo Первый запуск: устанавливаю зависимости...
  %PY% -m pip install -r requirements.txt
)

%PY% run.py %*
if errorlevel 1 pause
