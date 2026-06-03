@echo off
REM Chat Manager — Debug launcher (shows console output)
REM Double-click chat-manager.vbs for silent launch (no console)

cd /d "%~dp0"
echo Starting Chat Manager...
echo.

REM Try python3 first, then python
where python3 >nul 2>nul
if %errorlevel%==0 (
    python3 "%~dp0chat-manager-web.py"
) else (
    python "%~dp0chat-manager-web.py"
)
pause
