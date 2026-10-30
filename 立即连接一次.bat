@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Campus Network - connect now
echo ============================================
echo.
python connect.py
echo.
echo --------------------------------------------
echo Exit code: %ERRORLEVEL%  ^(0=OK, 1=failed, 2=config, 3=no portal^)
echo.
pause
