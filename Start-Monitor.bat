@echo off
setlocal EnableExtensions
REM Launcher: dock + history + tray. Double-click this file.

cd /d "%~dp0"
if not exist "monitor.py" (
    echo monitor.py not found here: %~dp0
    pause
    exit /b 1
)

REM 1) Typical per-user Python installs (PATH often missing when opened from Explorer)
set "_TRY=%LocalAppData%\Programs\Python\Python314\pythonw.exe"
if exist "%_TRY%" goto :run_one
set "_TRY=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
if exist "%_TRY%" goto :run_one
set "_TRY=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
if exist "%_TRY%" goto :run_one
set "_TRY=%LocalAppData%\Programs\Python\Python311\pythonw.exe"
if exist "%_TRY%" goto :run_one

REM 2) Windows "py" launcher — pyw = no console (same idea as pythonw)
if exist "%SystemRoot%\pyw.exe" (
    start "" "%SystemRoot%\pyw.exe" -3 "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)
if exist "%SystemRoot%\py.exe" (
    start "" "%SystemRoot%\py.exe" -3 "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)

REM 3) PATH fallbacks
where pyw >nul 2>&1 && (
    start "" pyw -3 "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)
where pythonw >nul 2>&1 && (
    start "" pythonw.exe "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)
where py >nul 2>&1 && (
    start "" py -3 "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)
where python >nul 2>&1 && (
    start "" python.exe "%~dp0monitor.py" --dock --history --tray
    exit /b 0
)

echo Could not find Python. Install from https://www.python.org/
echo Or run Start-Monitor-Debug.bat to see the exact error.
pause
exit /b 1

:run_one
start "" "%_TRY%" "%~dp0monitor.py" --dock --history --tray
exit /b 0
