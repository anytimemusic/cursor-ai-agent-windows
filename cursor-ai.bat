@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Environment .venv belum dibuat! Jalankan install.bat terlebih dahulu.
    exit /b 1
)

if "%~1"=="" (
    ".venv\Scripts\python.exe" "%~dp0cli.py" --help
    exit /b %errorlevel%
)

if /I "%~1"=="start" (
    shift
    call "%~dp0start.bat" %*
    exit /b %errorlevel%
)

".venv\Scripts\python.exe" "%~dp0cli.py" %*
exit /b %errorlevel%
