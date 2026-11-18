@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

title Kursor AI Agent - Installer Windows 11

echo ========================================================
echo   Kursor AI Agent for Windows 11 - Installer
echo ========================================================
echo.

REM --- 1. Cari Python 3.10+ yang valid (abaikan stub Microsoft Store) ---
set "PY_CMD="
set "PY_ARGS="

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARGS=-3"
    goto :python_found
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    set "PY_ARGS="
    goto :python_found
)

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LocalAppData%\Programs\Python\Python312\python.exe"
    goto :python_found
)
if exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
    set "PY_CMD=%LocalAppData%\Programs\Python\Python311\python.exe"
    goto :python_found
)

echo [INFO] Python 3.10+ belum ditemukan. Mencoba instalasi otomatis via winget...
where winget >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10+ dan winget tidak ditemukan.
    echo Silakan download & install Python 3.12 dari:
    echo   https://www.python.org/downloads/windows/
    echo PENTING: Centang "Add python.exe to PATH" saat instalasi!
    pause
    exit /b 1
)

winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    set "PY_CMD=%LocalAppData%\Programs\Python\Python312\python.exe"
    goto :python_found
)

py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARGS=-3"
    goto :python_found
)

echo [PERHATIAN] Python baru saja diinstall. Tutup jendela ini lalu jalankan ulang install.bat agar PATH ter-refresh.
pause
exit /b 0

:python_found
for /f "delims=" %%V in ('"%PY_CMD%" %PY_ARGS% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"') do set "PY_VER=%%V"
echo [OK] Menggunakan Python %PY_VER% (%PY_CMD% %PY_ARGS%)
echo.

REM --- 2. Buat Virtual Environment (.venv) ---
if not exist ".venv\Scripts\python.exe" (
    echo [INFO] Membuat virtual environment di .venv ...
    "%PY_CMD%" %PY_ARGS% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Gagal membuat virtual environment .venv
        pause
        exit /b 1
    )
) else (
    echo [OK] Virtual environment .venv sudah ada.
)

REM --- 3. Install dependensi Python ---
echo [INFO] Memperbarui pip dan menginstall paket dari requirements.txt ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Gagal menginstall dependensi Python. Cek koneksi internet Anda.
    pause
    exit /b 1
)
echo [OK] Semua paket Python berhasil terpasang!
echo.

REM --- 4. Cek / Install Tesseract OCR (sangat disarankan untuk Packet Tracer & UI OCR) ---
set "TESS_FOUND=0"
where tesseract >nul 2>&1 && set "TESS_FOUND=1"
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" set "TESS_FOUND=1"
if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" set "TESS_FOUND=1"
if exist "%LocalAppData%\Programs\Tesseract-OCR\tesseract.exe" set "TESS_FOUND=1"

if "%TESS_FOUND%"=="0" (
    echo [INFO] Tesseract OCR belum terpasang ^(dipakai untuk membaca label tab/CLI di Packet Tracer^&UI^).
    where winget >nul 2>&1
    if not errorlevel 1 (
        echo [INFO] Mencoba install Tesseract OCR via winget ...
        winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements
        if errorlevel 1 (
            echo [WARN] Gagal install otomatis Tesseract. Anda bisa install manual nanti dari:
            echo        https://github.com/UB-Mannheim/tesseract/wiki
        ) else (
            echo [OK] Tesseract OCR berhasil terpasang!
        )
    ) else (
        echo [WARN] winget tidak tersedia. Download manual Tesseract OCR di:
        echo        https://github.com/UB-Mannheim/tesseract/wiki
    )
) else (
    echo [OK] Tesseract OCR terdeteksi.
)
echo.

REM --- 5. Shortcut Start Menu & Desktop ---
set "STARTMENU_DIR=%AppData%\Microsoft\Windows\Start Menu\Programs"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('%STARTMENU_DIR%\Kursor AI Agent.lnk'); $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments = '\"%~dp0run.py\"'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Kursor AI Agent Overlay untuk Windows 11'; $s.Save()" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Shortcut Start Menu dibuat: Kursor AI Agent
)

if /I "%~1"=="--yes" (
    set "CREATE_DESKTOP=Y"
) else if /I "%~1"=="--no-shortcut" (
    set "CREATE_DESKTOP=N"
) else (
    choice /C YN /D Y /T 10 /M "Buat shortcut di Desktop juga (otomatis Y dalam 10 detik)?"
    if errorlevel 2 (set "CREATE_DESKTOP=N") else (set "CREATE_DESKTOP=Y")
)

if "%CREATE_DESKTOP%"=="Y" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $desk = [Environment]::GetFolderPath('Desktop'); $s = $ws.CreateShortcut((Join-Path $desk 'Kursor AI Agent.lnk')); $s.TargetPath = '%~dp0.venv\Scripts\pythonw.exe'; $s.Arguments = '\"%~dp0run.py\"'; $s.WorkingDirectory = '%~dp0'; $s.Description = 'Kursor AI Agent Overlay untuk Windows 11'; $s.Save()" >nul 2>&1
    if not errorlevel 1 (
        echo [OK] Shortcut Desktop dibuat!
    )
)

echo.
echo ========================================================
echo   INSTALASI SELESAI!
echo ========================================================
echo   Cara menjalankan:
echo     1. Double-click start.bat (atau shortcut Desktop/Start Menu)
echo     2. Tekan Ctrl+Alt+Space kapan saja untuk panggil/buka prompt bar
echo     3. Klik tab "Settings" di HUD untuk isi API Key ^(9router/OpenRouter/Gemini^)
echo     4. Shortcut penting Windows 11:
echo        - Ctrl+Alt+Space : Buka / Tutup HUD
echo        - Ctrl+Alt+P     : Pause / Lanjutkan agent
echo        - Ctrl+Shift+X   : Emergency STOP
echo ========================================================
echo.
if /I not "%~1"=="--yes" pause
exit /b 0
