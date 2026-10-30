@echo off
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -Command "Get-Content '%~dp0logs\connect.log' -Tail 40 -Encoding UTF8"
echo.
pause
