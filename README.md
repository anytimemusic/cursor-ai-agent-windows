# Kursor AI Agent for Windows 11

Asisten AI otonom berbentuk **kursor AI melayang 60fps** di layar Windows 11 Anda. Anda bisa memberi perintah lewat prompt bar yang menempel tepat di samping kursor AI (atau lewat CLI), lalu agen akan melihat layar (UI Automation + OCR + Vision), menggerakkan kursor, klik, drag, mengetik, menjalankan PowerShell, hingga mengonfigurasi topologi & CLI **Cisco Packet Tracer (termasuk soal `.pka`)**.

---

## Fitur Utama

- **Overlay Kursor AI Transparan 60fps (PySide6 + Win32)**:
  - Menempel 1:1 di samping ujung kursor AI tanpa delay saat bergerak (support multi-monitor & **Windows 11 Per-Monitor DPI Awareness V2**, tetap akurat di scaling `100%`, `125%`, `150%`, `200%`).
  - Kapsul status ringkas (`Type here...`) yang otomatis terbuka saat diarahkan mouse atau saat menekan **`Ctrl+Alt+Space`**.
  - Tombol **`×`** untuk menyembunyikan kursor AI ke System Tray kapan saja saat tidak dibutuhkan, dan tombol **`■ Stop`** / hotkey untuk hentikan paksa seketika.
  - Panel **Settings (`⚙`)** langsung di dalam kapsul HUD untuk mengganti provider, model, API key, warna aksen, dan kecepatan gerak.
- **Persepsi Layar & Self-Correction**:
  - Menggabungkan **Windows UI Automation**, **Tesseract OCR**, dan **Set-of-Mark (SoM)** visual screenshot.
  - Memverifikasi hasil klik/aksi setiap langkah — jika jendela/tab belum berpindah, klik meleset, atau perintah gagal, agen membaca ulang layar lalu mengoreksi tindakannya.
- **Otomasi Cisco Packet Tracer & `.pka`**:
  - Kalibrasi otomatis area canvas, panel kategori, dan bar navigasi di Windows 11 (menghormati area Taskbar Windows 11).
  - Menambah perangkat (Router, Switch, PC, Server, Laptop, WLC), memasang kabel otomatis/spesifik port (`FastEthernet`, `GigabitEthernet`, `Serial`), membaca soal `.pka`, serta mengisi konfigurasi **IOS CLI** (VLAN, Trunk, OSPF, DHCP, NAT, ACL, Static Route, dll).
- **Paralel Visual (Swarm Clones)**:
  - Bisa memecah tugas riset/konfigurasi multi-step menjadi beberapa klon kursor paralel dengan progress label masing-masing.
- **Multi-Provider AI**:
  - Mendukung **9router** (lokal, dengan failover otomatis antar model saat limit), **OpenRouter**, **Gemini (OpenAI-Compatible)**, **OpenAI**, dan **Ollama** lokal.

---

## Prasyarat Sistem

- **OS**: Windows 11 (64-bit)
- **Python**: 3.10 atau lebih baru (bisa diinstall otomatis oleh `install.bat` via `winget`)
- **Tesseract OCR**: Disarankan agar pembacaan tab, menu, dan CLI Packet Tracer akurat (juga diinstall otomatis oleh `install.bat` via `winget`)
- **AI Model**: Salah satu dari 9router lokal, OpenRouter API Key, Gemini API Key, OpenAI API Key, atau Ollama lokal

---

## Cara Install di Windows 11

### Opsi A — Instalasi Otomatis 1-Klik (Disarankan)

1. Download repository ini (klik tombol hijau **Code → Download ZIP**, lalu **Extract All**) atau clone lewat Git:
   ```powershell
   git clone https://github.com/anytimemusic/cursor-ai-agent-windows.git
   cd cursor-ai-agent-windows
   ```
   *(Sesuaikan URL repo GitHub jika Anda melakukan fork.)*
2. Double-click file **`install.bat`**.
3. Script akan otomatis:
   - Mengecek Python 3.10+ (atau menawarkan install Python 3.12 via `winget` bila belum ada).
   - Membuat virtual environment `.venv` dan menginstall dependensi (`PySide6`, `mss`, `Pillow`, `numpy`, `uiautomation`).
   - Mengecek / menginstall **Tesseract OCR**.
   - Membuat shortcut **Kursor AI Agent** di Start Menu (dan Desktop jika disetujui).

### Opsi B — Instalasi Manual (PowerShell)

```powershell
git clone https://github.com/anytimemusic/cursor-ai-agent-windows.git
cd cursor-ai-agent-windows

py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

# Install Tesseract OCR (jika belum terpasang):
winget install -e --id UB-Mannheim.TesseractOCR
```

---

## Cara Update (Untuk yang Sudah Download)

### Jika sudah ada file `update.bat`
Double-click **`update.bat`** — script akan otomatis menghentikan instance yang sedang berjalan, mengambil update terbaru dari GitHub (baik lewat `git pull` maupun download ZIP otomatis), memperbarui paket Python bila perlu, lalu menjaga `.venv` dan API Key Anda tetap aman.

### Jika Anda download sebelum ada `update.bat`
- **Jika dulu install lewat `git clone`:**
  ```powershell
  cd cursor-ai-agent-windows
  .\.venv\Scripts\python.exe cli.py quit
  git pull origin main
  ```
- **Jika dulu install lewat Download ZIP (tanpa Git):**
  1. Download ulang ZIP terbaru dari GitHub (**Code → Download ZIP**), ekstrak dan timpa folder `cursor-ai-agent-windows` lama Anda (atau cukup jalankan perintah PowerShell ini di dalam folder `cursor-ai-agent-windows`):
     ```powershell
     powershell -NoProfile -Command "$z=\"$env:TEMP\ka.zip\"; $e=\"$env:TEMP\ka_ext\"; Invoke-WebRequest 'https://github.com/anytimemusic/cursor-ai-agent-windows/archive/refs/heads/main.zip' -OutFile $z; Expand-Archive $z $e -Force; $s=\"$e\cursor-ai-agent-windows-main\"; Get-ChildItem $s -Recurse -File -Force | ForEach-Object { $r=$_.FullName.Substring($s.Length).TrimStart('\','/'); $t=Join-Path (Get-Location) $r; $d=Split-Path $t -Parent; if(!(Test-Path $d)){New-Item -ItemType Directory $d -Force|Out-Null}; Copy-Item $_.FullName $t -Force }; Remove-Item $z,$e -Recurse -Force"
     ```
  2. Konfigurasi API Key di `%APPDATA%\cursor-ai-agent\config.json` dan folder `.venv` tidak akan terhapus. Setelah itu jalankan `start.bat` (dan untuk update berikutnya cukup double-click `update.bat`).

---

## Cara Menjalankan & Penggunaan Dasar

1. Jalankan lewat shortcut **Kursor AI Agent** di Desktop/Start Menu, atau double-click **`start.bat`**.
   - Untuk melihat log debug di terminal:
     ```bat
     start.bat --console
     ```
   - Untuk mulai tersembunyi di System Tray:
     ```bat
     start.bat --tray
     ```
2. Arahkan kursor mouse ke kapsul **`Type here...`** di dekat AI kursor (atau tekan **`Ctrl+Alt+Space`**) untuk membuka prompt bar.
3. Sebelum perintah pertama, klik ikon **`⚙` (Settings)** di prompt bar untuk memilih provider AI, mengisi API Key, lalu klik **`Cek Model`** dan **`Simpan & Apply`**:
   - **OpenRouter**: Pilih `openrouter`, isi API Key `sk-or-...`, klik **`Cek Model`** untuk memilih model vision (mis. `google/gemini-2.5-flash` atau `anthropic/claude-sonnet-4`), lalu klik **`Simpan & Apply`**.
   - **Gemini**: Pilih `gemini`, isi Gemini API Key Anda, pilih/biarkan model auto (mis. `gemini-2.5-flash`), lalu klik **`Simpan & Apply`**.
   - **9router (Local)**: Pastikan 9router berjalan di `http://localhost:20128/v1`. Jika muncul status merah *9router belum aktif / WinError 10061*, jalankan 9router terlebih dahulu atau ganti Provider ke Gemini / OpenRouter / OpenAI.
   - **Ollama (Local)**: Pastikan Ollama berjalan di `http://localhost:11434/v1` dengan model multimodal (mis. `qwen2.5vl`).
   - File konfigurasi disimpan lokal di `%APPDATA%\cursor-ai-agent\config.json` (tidak pernah ikut ter-upload ke GitHub).
4. Ketik instruksi Anda lalu tekan **Enter** atau klik **`Run ▶`**.

---

## Pintasan Keyboard & Kontrol Windows 11

| Pintasan / Tombol | Fungsi |
| :--- | :--- |
| **`Ctrl + Alt + Space`** | Buka / Tutup prompt bar, atau munculkan kembali kursor AI jika sebelumnya ditutup dengan `×` |
| **`Ctrl + Alt + P`** | Pause / Lanjutkan aksi agent yang sedang berjalan |
| **`Ctrl + Shift + X`** | **Emergency STOP** — hentikan paksa semua aksi mouse/keyboard agent |
| Tombol **`×`** di kapsul | Sembunyikan kursor AI ke System Tray |
| **System Tray Icon** | Klik kanan/kiri ikon di pojok kanan bawah Windows 11 untuk Toggle HUD, Demo, Pause, Stop, atau Keluar |
| **Drag kapsul** | Seret kapsul `Type here...` saat idle untuk memindahkan posisi AI kursor |

---

## Perintah CLI (`cursor-ai.bat`)

Selain lewat overlay HUD, Anda bisa mengontrol agent dari Command Prompt / PowerShell:

```bat
cursor-ai.bat status
cursor-ai.bat toggle
cursor-ai.bat summon
cursor-ai.bat ask "Buka Notepad lalu tulis catatan rapat hari ini"
cursor-ai.bat pause
cursor-ai.bat halt
cursor-ai.bat demo
cursor-ai.bat quit
```

Atau jalankan tanpa UI overlay (hanya bridge TCP + CLI):
```bat
start.bat --headless
```

---

## Tips Pakai untuk Cisco Packet Tracer / Soal `.pka`

1. Buka jendela **Cisco Packet Tracer** di layar utama dalam kondisi terlihat penuh (tidak minimized dan tidak tertutup jendela lain).
2. Jika memakai file `.pka`, buka dulu file `.pka` dan pastikan panel instruksi soal terlihat atau beri tahu agent untuk membaca instruksinya.
3. Aktifkan opsi **Always Show Port Labels** di *Options → Preferences* Packet Tracer jika ingin verifikasi port lebih cepat.
4. Contoh prompt yang bisa langsung diketik:
   > *"Baca instruksi soal Packet Tracer yang sedang terbuka, tambahkan 1 Switch 2960 dan 2 PC, hubungkan dengan kabel straight ke Fa0/1 dan Fa0/2, lalu atur IP kedua PC di subnet 192.168.10.0/24 dan cek ping berhasil."*

---

## Troubleshooting Windows 11

- **"Windows protected your PC" (SmartScreen)** saat membuka `.bat`: Klik *More info → Run anyway* (file `.bat` adalah script teks terbuka yang bisa Anda inspect langsung dengan Notepad).
- **Koordinat klik kurang pas pada aplikasi Admin (Administrator)**: Karena alasan keamanan Windows UIPI, aplikasi non-admin tidak dapat mengirim input klik ke jendela yang berjalan *As Administrator*. Klik kanan `start.bat` → **Run as administrator** jika Anda ingin AI mengontrol aplikasi elevated.
- **OCR gagal / "Tesseract tidak ditemukan"**: Pastikan Tesseract terinstall di `C:\Program Files\Tesseract-OCR\tesseract.exe` atau tambahkan ke System `PATH`, lalu buka ulang `start.bat`.
- **Scaling Layar Laptop 125% / 150%**: Aplikasi sudah mengaktifkan *Per-Monitor DPI Awareness V2* secara native sehingga screenshot, UI Automation, dan input kursor bekerja pada koordinat piksel fisik 1:1.
- **Log Debug & Diagnostik**: Cek file `%LOCALAPPDATA%\cursor-ai-agent\trace.log` jika ingin melihat detail langkah tool call, model fallback, atau error API.

---

## Struktur Direktori

```text
cursor-ai-agent-windows/
├── run.py                  # Entrypoint utama Windows 11 (--tray / --headless / single-instance)
├── cli.py                  # CLI TCP client untuk status, ask, toggle, summon, pause, halt
├── install.bat             # Installer otomatis Python + .venv + Tesseract + Shortcut
├── update.bat              # Updater 1-klik untuk pengguna Git maupun Download ZIP
├── start.bat               # Launcher background tanpa jendela konsol (pythonw)
├── cursor-ai.bat           # Wrapper CLI untuk cmd/PowerShell
├── requirements.txt        # Dependensi Python Windows 11
├── agent/
│   ├── config.py           # Konfigurasi & preset provider (%APPDATA%\cursor-ai-agent)
│   ├── win32_backend.py    # Win32 API: DPI V2, SendInput, EnumWindows, DWM bounds, mss, clipboard
│   ├── perception.py       # UI Automation + Tesseract OCR + Set-of-Mark annotations
│   ├── desktop.py          # Kontroler kursor AI + Bezier glide + input shield
│   ├── packet_tracer.py    # Otomasi & kalibrasi Cisco Packet Tracer / .pka untuk Windows 11
│   ├── llm.py              # Client OpenAI-compatible + SSE + failover error handling
│   ├── tools.py            # Tool executor (mouse, keyboard, PowerShell, window, PT, swarm)
│   ├── runner.py           # Loop otonom AI + self-correction + 9router failover
│   └── bridge.py           # Event bus & TCP loopback control server
├── ui/
│   └── overlay.py          # Overlay PySide6 60fps, morphing HUD, Settings, Tray, Global Hotkey
└── tests/
    └── test_agent.py       # Unit & integration test suite
```
