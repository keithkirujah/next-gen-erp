@echo off
setlocal
cd /d "%~dp0"
title NextGen ERP Desktop

echo ================================================
echo          NEXTGEN ERP DESKTOP
echo ================================================
echo.
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
  )
)
if not defined PYEXE (
  echo ERROR: Python 3.10 or newer was not found.
  echo Install Python from https://www.python.org/downloads/windows/
  echo Enable "Add Python to PATH" during installation, then run this launcher again.
  pause
  exit /b 1
)
%PYEXE% -c "import importlib.util,sys; sys.exit(0 if all(importlib.util.find_spec(x) for x in ('PySide6','reportlab')) else 1)" >nul 2>nul
if errorlevel 1 (
  echo One or more required packages are missing. Installing declared dependencies...
  %PYEXE% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo ERROR: Dependency installation failed. No application files were changed.
    pause
    exit /b 1
  )
)
%PYEXE% -m pip check
if errorlevel 1 (
  echo.
  echo ERROR: Python dependency conflicts were detected. The ERP will not be started.
  echo Review the environment or use a clean virtual environment.
  pause
  exit /b 1
)
%PYEXE% -c "import PySide6, reportlab" >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: A required package is installed but cannot be imported.
  echo The launcher will not repeatedly reinstall packages automatically.
  echo Repair the Python environment and try again.
  pause
  exit /b 1
)
echo Starting NextGen ERP...
%PYEXE% main.py
if errorlevel 1 (
  echo.
  echo The ERP exited with an error. Review the message above.
  pause
)
endlocal
