@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Environment .venv belum dibuat!
    echo Silakan jalankan install.bat terlebih dahulu.
    pause
    exit /b 1
)

if /I "%~1"=="--console" (
    shift
    ".venv\Scripts\python.exe" "%~dp0run.py" %*
    exit /b %errorlevel%
)

if /I "%~1"=="--headless" (
    ".venv\Scripts\python.exe" "%~dp0run.py" %*
    exit /b %errorlevel%
)

start "" ".venv\Scripts\pythonw.exe" "%~dp0run.py" %*
exit /b 0
