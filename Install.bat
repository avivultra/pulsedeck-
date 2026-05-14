@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

REM ============================================================
REM  PulseDeck — One-click installer for Windows
REM
REM  What it does:
REM   1. Verifies Python 3.10+ is installed (prompts if not)
REM   2. Installs dependencies (psutil, matplotlib, pystray, Pillow)
REM   3. Resets any leftover personal config from the source machine
REM   4. Creates a desktop shortcut named "PulseDeck"
REM   5. Optionally launches the monitor in the background
REM ============================================================

title PulseDeck Installer

echo.
echo ============================================================
echo   PulseDeck Installer
echo   ====================
echo.
echo   This will install PulseDeck on this computer:
echo   - Verify Python is installed
echo   - Install required Python packages
echo   - Create a desktop shortcut
echo.
echo ============================================================
echo.
pause

REM --- 1. Find Python -----------------------------------------------------
set "PYTHON_EXE="
for %%P in (
  "%LocalAppData%\Programs\Python\Python314\python.exe"
  "%LocalAppData%\Programs\Python\Python313\python.exe"
  "%LocalAppData%\Programs\Python\Python312\python.exe"
  "%LocalAppData%\Programs\Python\Python311\python.exe"
  "%LocalAppData%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
) do (
  if exist %%P (
    set "PYTHON_EXE=%%P"
    goto :have_python
  )
)

REM Fall back to whatever is on PATH
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
  for /f "delims=" %%i in ('where python') do (
    set "PYTHON_EXE=%%i"
    goto :have_python
  )
)

echo.
echo ============================================================
echo   ERROR: Python is not installed.
echo.
echo   Please install Python 3.10 or newer from:
echo     https://www.python.org/downloads/
echo.
echo   IMPORTANT: During installation, check the box
echo   "Add Python to PATH" on the first screen.
echo.
echo   Then run this Install.bat again.
echo ============================================================
echo.
pause
exit /b 1

:have_python
echo [1/4] Python found: !PYTHON_EXE!

REM --- 2. Install dependencies -------------------------------------------
echo.
echo [2/4] Installing required packages (psutil, matplotlib, pystray, Pillow)...
echo       This may take 1-2 minutes the first time...
echo.
"!PYTHON_EXE!" -m pip install --upgrade pip --quiet
"!PYTHON_EXE!" -m pip install -r "%~dp0requirements.txt" --quiet
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo ERROR: pip install failed. Re-run this file or check your internet.
  pause
  exit /b 1
)
echo       Dependencies OK.

REM --- 3. Reset personal config from the source machine ------------------
echo.
echo [3/4] Resetting personal config (if any) so dock auto-places...
if exist "%~dp0config.json" (
  del /q "%~dp0config.json"
  echo       Removed leftover config.json — it will be re-created with defaults on first run.
) else (
  echo       No config.json found — clean install.
)

REM Also clear any history left over from the previous machine
if exist "%~dp0history\regular" (
  rmdir /s /q "%~dp0history\regular"
  echo       Removed old history\regular (previous machine's metrics).
)
if exist "%~dp0history\spikes" (
  rmdir /s /q "%~dp0history\spikes"
  echo       Removed old history\spikes (previous machine's alerts).
)
if exist "%~dp0history\monitor.log" del /q "%~dp0history\monitor.log"
if exist "%~dp0history\janitor.log" del /q "%~dp0history\janitor.log"

REM --- 4. Create the desktop shortcut ------------------------------------
echo.
echo [4/4] Creating desktop shortcut "PulseDeck"...
"!PYTHON_EXE!" "%~dp0install_shortcut.py"
if %ERRORLEVEL% NEQ 0 (
  echo.
  echo WARNING: Shortcut creation failed. You can still launch the monitor
  echo by double-clicking Start-Monitor-Hidden.vbs in this folder.
)

REM --- Done --------------------------------------------------------------
echo.
echo ============================================================
echo   Installation complete!
echo.
echo   A shortcut named "PulseDeck" was created on your desktop.
echo   Double-click it to launch the monitor.
echo.
echo   To start the monitor right now, press any key.
echo   To finish without launching, close this window.
echo ============================================================
echo.
pause >nul

REM Launch in hidden mode
start "" "%~dp0Start-Monitor-Hidden.vbs"
echo.
echo PulseDeck is running in the background.
echo Look for the dock above your taskbar, or the tray icon by the clock.
echo.
timeout /t 3 >nul
exit /b 0
