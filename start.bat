@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
endlocal
pause
