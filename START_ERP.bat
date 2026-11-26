@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title NextGen ERP Desktop

echo ================================================
echo          NEXTGEN ERP DESKTOP
echo ================================================
echo.

set "VENV=%~dp0.venv"
set "PYEXE=%VENV%\Scripts\python.exe"

if not exist "%PYEXE%" (
  echo Creating isolated ERP Python environment...
  set "BASEPY="
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
    if not errorlevel 1 set "BASEPY=py -3"
  )
  if not defined BASEPY (
    where python >nul 2>nul
    if not errorlevel 1 (
      python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
      if not errorlevel 1 set "BASEPY=python"
    )
  )
  if not defined BASEPY (
    echo ERROR: Python 3.10 or newer not found.
    echo Install from https://www.python.org/downloads/windows/
    pause
    exit /b 1
  )
  !BASEPY! -m venv "%VENV%"
  if errorlevel 1 (
    echo ERROR: Could not create the ERP virtual environment.
    pause
    exit /b 1
  )
)

if not exist "%PYEXE%" (
  echo ERROR: The ERP virtual environment is incomplete.
  pause
  exit /b 1
)

"%PYEXE%" -c "import PySide6,reportlab" >nul 2>nul
if errorlevel 1 (
  echo Installing only declared ERP dependencies into .venv...
  "%PYEXE%" -m pip install --upgrade pip
  "%PYEXE%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo ERROR: Installation failed. Check internet connection.
    pause
    exit /b 1
  )
)

echo Launching NextGen ERP...
"%PYEXE%" main.py
if errorlevel 1 (
  echo.
  echo ERP exited with an error. Review the traceback/log.
  pause
)
endlocal
