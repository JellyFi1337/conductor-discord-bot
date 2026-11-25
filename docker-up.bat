@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0docker-up.ps1" %*
set ERR=%ERRORLEVEL%
if %ERR% neq 0 pause
endlocal & exit /b %ERR%
