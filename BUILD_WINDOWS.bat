@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Build NextGen ERP

set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
  echo First run START_ERP.bat to create the isolated environment.
  pause
  exit /b 1
)
"%PYEXE%" -m pip install -r requirements-build.txt
if errorlevel 1 (
  echo ERROR: Build tools could not be installed.
  pause
  exit /b 1
)
"%PYEXE%" -m PyInstaller --clean --noconfirm --windowed --onedir --name "NextGen ERP" --icon "assets\nextgen-erp-mark.ico" main.py
if errorlevel 1 (
  echo BUILD FAILED. See PyInstaller output.
  pause
  exit /b 1
)
echo.
echo BUILD SUCCESS. Distribution folder: dist\NextGen ERP
echo Copy the entire folder; do not copy only the EXE.
pause
endlocal
