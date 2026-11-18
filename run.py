from __future__ import annotations

import argparse
import ctypes
import json
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import win32_backend as wb
from agent.bridge import PORT_FILE, AgentBridge


def _show_windows_error(title: str, message: str) -> None:
    print(f"[{title}] {message}", file=sys.stderr)
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
        except Exception:
            pass


def _probe_existing_instance(timeout: float = 0.6) -> int | None:
    """Return listening TCP port if an active Kursor AI Agent instance is already running."""
    if not PORT_FILE.exists():
        return None
    try:
        port = int(PORT_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None
    if port <= 0 or port > 65535:
        return None

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps({"cmd": "status"}) + "\n").encode("utf-8"))
            raw = sock.recv(65536).decode("utf-8", errors="replace").strip()
            if raw:
                resp = json.loads(raw.splitlines()[0])
                if isinstance(resp, dict) and resp.get("ok"):
                    return port
    except Exception:
        return None
    return None


def _send_to_existing(port: int, payload: dict[str, object], timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            _ = sock.recv(65536)
        return True
    except Exception:
        return False


def _run_headless() -> int:
    stop_event = threading.Event()

    def _on_event(ev: dict[str, object]) -> None:
        if ev.get("type") == "ui_command" and ev.get("action") == "quit":
            stop_event.set()
        print(json.dumps(ev, ensure_ascii=False), flush=True)

    bridge = AgentBridge(event_listener=_on_event)
    port = bridge.start()
    print(f"Kursor AI Agent headless bridge listening on 127.0.0.1:{port}", flush=True)
    try:
        while not stop_event.wait(timeout=0.4):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        bridge.shutdown()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kursor AI Agent for Windows 11")
    parser.add_argument(
        "--tray",
        "--hidden",
        dest="tray",
        action="store_true",
        help="Mulai tersembunyi di System Tray (panggil lewat Ctrl+Alt+Space atau CLI)",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan bridge TCP tanpa overlay Qt (untuk CLI atau otomasi)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Paksa jalankan instance baru meskipun instance lain terdeteksi aktif",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.force:
        existing_port = _probe_existing_instance()
        if existing_port is not None:
            if args.headless:
                print(
                    f"Instance Kursor AI Agent sudah berjalan di port {existing_port}. "
                    "Gunakan cli.py atau --force.",
                    file=sys.stderr,
                )
                return 0
            if not args.tray:
                _send_to_existing(existing_port, {"cmd": "ui", "action": "open"})
            return 0
        try:
            if PORT_FILE.exists():
                PORT_FILE.unlink()
        except OSError:
            pass

    if args.headless:
        wb.enable_dpi_awareness()
        return _run_headless()

    try:
        from ui.overlay import run_overlay_app
    except ModuleNotFoundError as e:
        _show_windows_error(
            "Dependensi Belum Lengkap",
            f"Paket Python belum terpasang ({e}).\n\n"
            "Jalankan install.bat terlebih dahulu atau jalankan:\n"
            "pip install -r requirements.txt",
        )
        return 1

    return run_overlay_app(start_dismissed=bool(args.tray))


if __name__ == "__main__":
    raise SystemExit(main())
