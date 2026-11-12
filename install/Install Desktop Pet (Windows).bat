@echo off
rem One-click installer: Python packages in a private environment, a Desktop shortcut
rem with the pet icon, then starts the pet. Safe to run again at any time.
rem No ( ) blocks around paths below: a ")" in a folder name would break them.
setlocal
title Desktop Pet installer
cd /d "%~dp0.."
if not exist "desktop_cat.py" goto :no_script

set "VENV=%LOCALAPPDATA%\DesktopCat\venv"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"

echo.
echo   Installing Desktop Pet
echo   ======================
echo.
echo [1/4] Looking for Python 3.10 or newer...
if exist "%VENV%\Scripts\python.exe" goto :packages
set "PY="
py -3 -c "%CHECK%" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "%CHECK%" >nul 2>&1 && set "PY=python"
if defined PY goto :make_venv

echo       Python not found. Installing it now...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" goto :no_python
set "PY="%PY%""

:make_venv
echo [2/4] Creating a private Python environment...
if not exist "%LOCALAPPDATA%\DesktopCat" mkdir "%LOCALAPPDATA%\DesktopCat"
%PY% -m venv "%VENV%"
if errorlevel 1 goto :failed

:packages
echo [3/4] Installing required packages (this can take a minute)...
"%VENV%\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :failed

echo [4/4] Creating the Desktop shortcut and starting the pet...
"%VENV%\Scripts\python.exe" desktop_cat.py --install-shortcut
if errorlevel 1 goto :failed
start "" "%VENV%\Scripts\pythonw.exe" "%CD%\desktop_cat.py"

echo.
echo   Done! Look for your pet above your taskbar.
echo   Next time, use the "Desktop Pet" shortcut on your Desktop.
echo.
timeout /t 8 >nul
exit /b 0

:no_script
echo.
echo   desktop_cat.py was not found. Keep this "install" folder inside the
echo   Desktop Pet folder, and unzip the download first if it is still zipped.
goto :stop

:no_python
echo.
echo   Python could not be installed automatically. Install Python 3.10 or newer
echo   from https://www.python.org/downloads/ and run this file again.
goto :stop

:failed
echo.
echo   Something went wrong. Read the message above, then run this file again.
echo   If it mentions the internet or a download, check your connection.

:stop
echo.
pause
exit /b 1
