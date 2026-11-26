@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."
title NextGen ERP — build Windows EXE

echo ============================================================
echo   NextGen ERP Desktop — Windows executable build
echo ============================================================
echo.
echo Working folder: %CD%
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
  echo ERROR: Python 3.10+ not found. Install from https://www.python.org/downloads/windows/
  echo Enable "Add Python to PATH", then run this script again.
  pause
  exit /b 1
)

echo [1/4] Installing runtime + PyInstaller...
%PYEXE% -m pip install -r requirements.txt pyinstaller --upgrade
if errorlevel 1 (
  echo ERROR: pip install failed.
  pause
  exit /b 1
)

echo.
echo [2/4] Quick import check...
%PYEXE% -c "import main, operations, printing, document_templates, erp_dashboard; print('OK')"
if errorlevel 1 (
  echo ERROR: Application imports failed. Fix errors above, then rebuild.
  pause
  exit /b 1
)

echo.
echo [3/4] Building NextGenERP.exe (this can take several minutes)...
if exist build rmdir /s /q build
if exist dist\NextGenERP rmdir /s /q dist\NextGenERP
%PYEXE% -m PyInstaller packaging\NextGenERP.spec --noconfirm --clean
if errorlevel 1 (
  echo ERROR: PyInstaller failed.
  pause
  exit /b 1
)

if not exist "dist\NextGenERP\NextGenERP.exe" (
  echo ERROR: dist\NextGenERP\NextGenERP.exe was not created.
  pause
  exit /b 1
)

echo.
echo [4/4] Optional Inno Setup installer (skipped if ISCC not installed)...
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" packaging\installer.iss
) else (
  echo Inno Setup not found — folder build only. EXE is ready without an installer.
)

echo.
echo ============================================================
echo   BUILD COMPLETE
echo.
echo   Run the app:
echo     dist\NextGenERP\NextGenERP.exe
echo.
echo   Ship the whole folder:
echo     dist\NextGenERP\
echo ============================================================
explorer dist\NextGenERP
pause
