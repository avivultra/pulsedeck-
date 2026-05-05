@echo off
REM Launcher for the performance monitor (dock + history + tray).
REM Double-click this file. Requires Python on PATH (pythonw preferred).
REM For almost no console flash, double-click: Start-Monitor-Hidden.vbs

cd /d "%~dp0"
if not exist "monitor.py" (
    echo monitor.py not found in this folder.
    pause
    exit /b 1
)

where pythonw >nul 2>&1
if %ERRORLEVEL% equ 0 (
    start "" pythonw.exe monitor.py --dock --history --tray
    exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    start "" python.exe monitor.py --dock --history --tray
    exit /b 0
)

where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    start "" py -3 monitor.py --dock --history --tray
    exit /b 0
)

echo Python was not found. Install from https://www.python.org/
echo and enable "Add python.exe to PATH", then run:
echo   pip install -r requirements.txt
pause
exit /b 1
