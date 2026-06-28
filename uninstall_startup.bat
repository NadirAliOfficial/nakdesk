@echo off
schtasks /delete /tn "NakDesk Host" /f >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] NakDesk auto-start removed.
) else (
    echo [FAIL] Task not found or run as Administrator.
)
pause
