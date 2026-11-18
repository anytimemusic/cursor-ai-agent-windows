from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.bridge import PORT_FILE


def send_command(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    """Send a single JSON command to the running Kursor AI Agent TCP bridge."""
    if not PORT_FILE.exists():
        raise RuntimeError(
            f"Kursor AI Agent belum berjalan (file port tidak ditemukan: {PORT_FILE}). "
            "Jalankan start.bat terlebih dahulu."
        )
    try:
        port = int(PORT_FILE.read_text(encoding="utf-8").strip())
    except Exception as e:
        raise RuntimeError(f"File port rusak ({PORT_FILE}): {e}") from e

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    except OSError as e:
        raise RuntimeError(
            f"Gagal terhubung ke bridge Kursor AI Agent di 127.0.0.1:{port} ({e}). "
            "Pastikan start.bat sedang berjalan."
        ) from e

    if not raw:
        return {"ok": False, "error": "Respons kosong dari bridge"}
    return json.loads(raw.splitlines()[0])


def build_command_payload(args: argparse.Namespace) -> dict[str, Any]:
    cmd = args.command
    if cmd == "status":
        return {"cmd": "status"}
    if cmd == "ask":
        return {
            "cmd": "ask",
            "prompt": " ".join(args.prompt),
            "attach_screen": not bool(args.no_screen),
        }
    if cmd in ("toggle", "open", "close", "summon"):
        return {"cmd": "ui", "action": cmd}
    if cmd == "pause":
        if args.resume:
            return {"cmd": "resume"}
        if args.only_pause:
            return {"cmd": "pause"}
        return {"cmd": "toggle_pause"}
    if cmd in ("halt", "stop"):
        return {"cmd": "stop"}
    if cmd == "reset":
        return {"cmd": "reset"}
    if cmd == "demo":
        return {"cmd": "demo"}
    if cmd == "demo-swarm":
        return {"cmd": "demo_swarm"}
    if cmd == "move":
        payload: dict[str, Any] = {"cmd": "move_cursor", "x": args.x, "y": args.y}
        if args.label is not None:
            payload["label"] = args.label
        if args.silent:
            payload["silent"] = True
        return payload
    if cmd == "quit":
        return {"cmd": "quit"}
    raise ValueError(f"Perintah tidak dikenal: {cmd}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cursor-ai",
        description="CLI kontrol Kursor AI Agent untuk Windows 11",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Cek status agent, posisi kursor, dan daftar monitor")

    p_ask = sub.add_parser("ask", help="Kirim prompt/instruksi ke agent")
    p_ask.add_argument("--no-screen", action="store_true", help="Jangan sertakan screenshot awal")
    p_ask.add_argument("prompt", nargs="+", help="Instruksi untuk dijalankan AI kursor")

    sub.add_parser("toggle", help="Buka atau tutup prompt bar HUD")
    sub.add_parser("open", help="Buka prompt bar HUD")
    sub.add_parser("close", help="Tutup prompt bar HUD")
    sub.add_parser("summon", help="Panggil kembali AI kursor jika tersembunyi")

    p_pause = sub.add_parser("pause", help="Toggle pause / lanjutkan eksekusi agent")
    p_pause.add_argument("--pause", dest="only_pause", action="store_true", help="Paksa pause")
    p_pause.add_argument("--resume", action="store_true", help="Paksa lanjutkan")

    sub.add_parser("halt", aliases=["stop"], help="Hentikan paksa tugas agent (Emergency STOP)")
    sub.add_parser("reset", help="Reset riwayat percakapan agent")
    sub.add_parser("demo", help="Jalankan demo animasi gerak AI kursor")
    sub.add_parser("demo-swarm", help="Jalankan demo animasi kloning swarm kursor")

    p_move = sub.add_parser("move", help="Pindahkan AI kursor ke koordinat fisik (x, y)")
    p_move.add_argument("x", type=int, help="Koordinat X fisik")
    p_move.add_argument("y", type=int, help="Koordinat Y fisik")
    p_move.add_argument("--label", default=None, help="Label kapsul kursor")
    p_move.add_argument("--silent", action="store_true", help="Update koordinat tanpa animasi")

    sub.add_parser("quit", help="Matikan Kursor AI Agent sepenuhnya")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_command_payload(args)
    try:
        resp = send_command(payload)
    except RuntimeError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
