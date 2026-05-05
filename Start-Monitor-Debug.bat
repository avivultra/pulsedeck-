@echo off
REM If the normal launcher does nothing: run this to see errors in the window.

cd /d "%~dp0"
setlocal

set "_PY="
if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "_PY=%LocalAppData%\Programs\Python\Python314\python.exe"
if exist "%LocalAppData%\Programs\Python\Python313\python.exe" set "_PY=%LocalAppData%\Programs\Python\Python313\python.exe"
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "_PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "_PY=%LocalAppData%\Programs\Python\Python311\python.exe"

if defined _PY (
    "%_PY%" monitor.py --dock --history --tray
) else if exist "%SystemRoot%\py.exe" (
    "%SystemRoot%\py.exe" -3 "%~dp0monitor.py" --dock --history --tray
) else (
    python monitor.py --dock --history --tray
)
echo.
echo Exit code: %ERRORLEVEL%
pause
