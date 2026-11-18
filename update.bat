@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Kursor AI Agent - Updater Windows 11

echo ========================================================
echo   Kursor AI Agent for Windows 11 - Updater
echo ========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    echo [INFO] Menghentikan instance Kursor AI Agent yang sedang aktif...
    ".venv\Scripts\python.exe" cli.py quit >nul 2>&1
    timeout /t 1 /nobreak >nul
)

set "UPDATED=0"
if exist ".git" (
    where git >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] Mendeteksi repository Git. Mengambil update dari origin/main...
        git pull --ff-only origin main
        if not errorlevel 1 (
            set "UPDATED=1"
        ) else (
            echo [WARN] git pull gagal ^(kemungkinan ada perubahan lokal^). Mencoba update via ZIP...
        )
    )
)

if "%UPDATED%"=="0" (
    echo [INFO] Mengunduh versi terbaru dari GitHub...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
        "$ErrorActionPreference='Stop';" ^
        "$zip=Join-Path $env:TEMP 'kursor_ai_update.zip';" ^
        "$ext=Join-Path $env:TEMP 'kursor_ai_extract';" ^
        "if(Test-Path $zip){Remove-Item $zip -Force};" ^
        "if(Test-Path $ext){Remove-Item $ext -Recurse -Force};" ^
        "Invoke-WebRequest -Uri 'https://github.com/anytimemusic/cursor-ai-agent-windows/archive/refs/heads/main.zip' -OutFile $zip;" ^
        "Expand-Archive -Path $zip -DestinationPath $ext -Force;" ^
        "$src=(Join-Path $ext 'cursor-ai-agent-windows-main');" ^
        "$dst=(Get-Location).Path;" ^
        "Get-ChildItem -LiteralPath $src -Recurse -File -Force | ForEach-Object {" ^
        "  $rel=$_.FullName.Substring($src.Length).TrimStart('\','/');" ^
        "  if($rel -eq 'update.bat'){$rel='update.bat.new'};" ^
        "  $target=Join-Path $dst $rel;" ^
        "  $dir=Split-Path $target -Parent;" ^
        "  if(-not (Test-Path $dir)){New-Item -ItemType Directory -Path $dir -Force | Out-Null};" ^
        "  Copy-Item -LiteralPath $_.FullName -Destination $target -Force" ^
        "};" ^
        "Remove-Item $zip -Force -ErrorAction SilentlyContinue;" ^
        "Remove-Item $ext -Recurse -Force -ErrorAction SilentlyContinue"
    if errorlevel 1 (
        echo [ERROR] Gagal mengunduh atau mengekstrak update. Cek koneksi internet Anda.
        pause
        exit /b 1
    )
)

if exist ".venv\Scripts\python.exe" (
    echo [INFO] Memastikan dependensi Python mutakhir...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
) else (
    echo [INFO] .venv belum ada. Menjalankan install.bat...
    call install.bat --no-shortcut
)

echo.
echo ========================================================
echo   UPDATE SELESAI! Konfigurasi ^& API Key Anda tetap aman.
echo   Jalankan start.bat untuk mulai memakai versi terbaru.
echo ========================================================
echo.

if exist "update.bat.new" (
    (
        move /y "update.bat.new" "update.bat" >nul
        if /I not "%~1"=="--yes" pause
        exit /b 0
    )
)

if /I not "%~1"=="--yes" pause
exit /b 0
