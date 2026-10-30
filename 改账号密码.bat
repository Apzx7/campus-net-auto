@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Campus Network - change account / password
echo ============================================
echo.
python connect.py --setup --interactive
echo.
echo --------------------------------------------
pause
