@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VBS_PATH=%SCRIPT_DIR%start_host.vbs"

schtasks /create ^
  /tn "NakDesk Host" ^
  /tr "wscript.exe \"%VBS_PATH%\"" ^
  /sc ONLOGON ^
  /delay 0000:15 ^
  /ru "%USERNAME%" ^
  /f >nul 2>&1

if %errorlevel% equ 0 (
    echo [OK] NakDesk will auto-start on every login.
    echo      Log file: %SCRIPT_DIR%nakdesk.log
) else (
    echo [FAIL] Run this script as Administrator.
)

pause
