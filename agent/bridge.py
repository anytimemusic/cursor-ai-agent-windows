from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .config import PROVIDER_PRESETS, load_config, save_config
from .desktop import DesktopController
from .llm import list_models
from .runner import AgentRunner


def get_port_file_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "cursor-ai-agent" / "bridge.port"
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "cursor-ai-agent-win.port"


PORT_FILE = get_port_file_path()


class AgentBridge:
    """In-process agent bridge + localhost TCP control server for Windows CLI/automation."""

    def __init__(self, event_listener: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._out_lock = threading.Lock()
        self._cfg_lock = threading.Lock()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        if event_listener is not None:
            self._listeners.append(event_listener)
        self._config = load_config()
        self.desktop = DesktopController(on_event=self.emit)
        self.runner = AgentRunner(self.desktop, self.get_config, self.emit)
        self._sock: socket.socket | None = None
        self._port: int = 0
        self._running = True

    def add_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        with self._out_lock:
            self._listeners.append(cb)

    def get_config(self) -> dict[str, Any]:
        with self._cfg_lock:
            return dict(self._config)

    def emit(self, payload: dict[str, Any]) -> None:
        with self._out_lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(payload)
            except Exception:
                pass

    def _check_models_async(self, base_url: str | None = None, api_key: str | None = None) -> None:
        def _worker() -> None:
            cfg = self.get_config()
            b = base_url if base_url is not None else str(cfg.get("base_url") or "")
            k = api_key if api_key is not None else str(cfg.get("api_key") or "")
            res = list_models(b, k, timeout=4.0)
            self.emit({"type": "models_status", **res})

        threading.Thread(target=_worker, daemon=True).start()

    def handle_command(self, msg: dict[str, Any]) -> dict[str, Any]:
        cmd = str(msg.get("cmd") or "").strip()

        if cmd == "ask":
            prompt = str(msg.get("prompt") or "")
            attach = bool(msg.get("attach_screen", False))
            ok = self.runner.run_prompt(prompt, attach_screen=attach)
            return {"ok": ok}

        if cmd == "pause":
            self.runner.pause()
            return {"ok": True}

        if cmd == "resume":
            self.runner.resume()
            return {"ok": True}

        if cmd == "toggle_pause":
            if self.runner.status == "paused":
                self.runner.resume()
            elif self.runner.status == "running":
                self.runner.pause()
            return {"ok": True, "status": self.runner.status}

        if cmd == "stop":
            self.runner.stop()
            return {"ok": True}

        if cmd == "reset":
            self.runner.reset_chat()
            return {"ok": True}

        if cmd == "save_config":
            updates = msg.get("config") or {}
            if isinstance(updates, dict):
                with self._cfg_lock:
                    self._config = save_config(updates)
                cfg = self.get_config()
                self.emit({"type": "config_updated", "config": cfg})
                self._check_models_async()
                return {"ok": True}
            return {"ok": False, "error": "invalid config"}

        if cmd == "check_models":
            self._check_models_async(msg.get("base_url"), msg.get("api_key"))
            return {"ok": True}

        if cmd == "move_cursor":
            x = int(msg.get("x", self.desktop.ai_cursor_x))
            y = int(msg.get("y", self.desktop.ai_cursor_y))
            if msg.get("silent"):
                self.desktop.ai_cursor_x = x
                self.desktop.ai_cursor_y = y
                return {"ok": True, "x": x, "y": y}
            label = str(msg.get("label") or "")
            self.desktop.move_ai_cursor(x, y, label=label, glide_ms=0, move_physical=False)
            return {"ok": True, "x": x, "y": y}

        if cmd == "demo":
            def _run_demo() -> None:
                mons = self.desktop.get_monitors()
                focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
                sx = int(focused["x"]) if focused else 0
                sy = int(focused["y"]) if focused else 0
                sw = int(focused["logical_w"]) if focused else 1920
                sh = int(focused["logical_h"]) if focused else 1080
                pts = [
                    (sx + int(sw * 0.28), sy + int(sh * 0.34), "Swish..."),
                    (sx + int(sw * 0.68), sy + int(sh * 0.26), "Checking..."),
                    (sx + int(sw * 0.74), sy + int(sh * 0.62), "Click!"),
                    (sx + int(sw * 0.38), sy + int(sh * 0.66), "Gliding..."),
                    (sx + int(sw * 0.50), sy + int(sh * 0.48), "Type here..."),
                ]
                for idx, (px, py, lbl) in enumerate(pts):
                    self.desktop.move_ai_cursor(px, py, label=lbl, glide_ms=160, move_physical=False)
                    if idx == 2:
                        self.emit({"type": "cursor_action", "action": "click", "button": "left", "x": px, "y": py})
                        time.sleep(0.12)

            threading.Thread(target=_run_demo, daemon=True).start()
            return {"ok": True}

        if cmd == "demo_swarm":
            def _run_swarm_demo() -> None:
                mons = self.desktop.get_monitors()
                focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
                sx = int(focused["x"]) if focused else 0
                sy = int(focused["y"]) if focused else 0
                sw = int(focused["logical_w"]) if focused else 1920
                sh = int(focused["logical_h"]) if focused else 1080
                mx, my = self.desktop.ai_cursor_x, self.desktop.ai_cursor_y
                clones = [
                    {"id": "clone-1", "x": mx, "y": my, "target_x": sx + int(sw * 0.25), "target_y": sy + int(sh * 0.28), "color": "#34d399", "label": "Klon #1 Web"},
                    {"id": "clone-2", "x": mx, "y": my, "target_x": sx + int(sw * 0.74), "target_y": sy + int(sh * 0.28), "color": "#f59e0b", "label": "Klon #2 Switch"},
                    {"id": "clone-3", "x": mx, "y": my, "target_x": sx + int(sw * 0.30), "target_y": sy + int(sh * 0.72), "color": "#a78bfa", "label": "Klon #3 Script"},
                ]
                self.emit({"type": "cursor_status", "label": "Belah 3 kursor..."})
                self.emit({"type": "clone_spawn", "clones": clones})
                time.sleep(0.45)
                waypoints = [
                    (sx + int(sw * 0.34), sy + int(sh * 0.42), "Klik YT..."),
                    (sx + int(sw * 0.65), sy + int(sh * 0.46), "Taruh 2960..."),
                    (sx + int(sw * 0.48), sy + int(sh * 0.74), "Run script..."),
                ]
                for i, c in enumerate(clones):
                    wx, wy, wlbl = waypoints[i]
                    self.emit({"type": "clone_move", "id": c["id"], "x": wx, "y": wy, "label": wlbl})
                time.sleep(0.32)
                for i, c in enumerate(clones):
                    wx, wy, _ = waypoints[i]
                    self.emit({"type": "clone_action", "id": c["id"], "action": "click", "x": wx, "y": wy})
                    time.sleep(0.10)
                time.sleep(0.35)
                for c in clones:
                    self.emit({"type": "clone_move", "id": c["id"], "x": mx, "y": my, "label": "Menyatu..."})
                time.sleep(0.35)
                self.emit({"type": "clone_merge"})
                self.emit({"type": "cursor_status", "label": "Type here..."})

            threading.Thread(target=_run_swarm_demo, daemon=True).start()
            return {"ok": True}

        if cmd == "ui":
            action = str(msg.get("action") or "toggle")
            teleported = False
            if action in ("toggle", "open", "summon"):
                try:
                    mons = self.desktop.get_monitors()
                    focused = next((m for m in mons if m.get("focused")), None)
                    if focused:
                        cx, cy = self.desktop.ai_cursor_x, self.desktop.ai_cursor_y
                        on_focused = (
                            int(focused["x"]) <= cx < int(focused["x"]) + int(focused["logical_w"])
                            and int(focused["y"]) <= cy < int(focused["y"]) + int(focused["logical_h"])
                        )
                        if not on_focused:
                            teleported = True
                            self.desktop.move_ai_cursor(
                                int(focused["x"]) + int(focused["logical_w"]) // 2,
                                int(focused["y"]) + int(focused["logical_h"]) // 2,
                                label="Type here...",
                                glide_ms=0,
                                move_physical=False,
                            )
                except Exception:
                    pass
            emit_action = "open" if (teleported and action == "toggle") else action
            self.emit({"type": "ui_command", "action": emit_action})
            return {"ok": True}

        if cmd == "quit":
            self.emit({"type": "ui_command", "action": "quit"})
            return {"ok": True}

        if cmd == "status":
            return {
                "ok": True,
                "status": self.runner.status,
                "cursor": [self.desktop.ai_cursor_x, self.desktop.ai_cursor_y],
                "monitors": self.desktop.get_monitors(),
            }

        return {"ok": False, "error": f"Unknown command: {cmd}"}

    def start(self) -> int:
        self.desktop.reset_physical_shield()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        srv.settimeout(0.5)
        self._sock = srv
        self._port = int(srv.getsockname()[1])

        PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PORT_FILE.write_text(str(self._port), encoding="utf-8")

        def _accept_loop() -> None:
            while self._running:
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    break
                with conn:
                    try:
                        data = conn.recv(65536).decode("utf-8", errors="replace")
                        replies = []
                        for line in data.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            obj = json.loads(line)
                            replies.append(self.handle_command(obj))
                        resp = json.dumps(replies[-1] if replies else {"ok": True}, ensure_ascii=False)
                        conn.sendall((resp + "\n").encode("utf-8"))
                    except Exception as e:
                        try:
                            conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode("utf-8"))
                        except Exception:
                            pass

        threading.Thread(target=_accept_loop, daemon=True).start()

        self.emit(
            {
                "type": "init",
                "config": self.get_config(),
                "presets": PROVIDER_PRESETS,
                "cursor": {"x": self.desktop.ai_cursor_x, "y": self.desktop.ai_cursor_y},
                "monitors": self.desktop.get_monitors(),
                "status": self.runner.status,
            }
        )
        self._check_models_async()
        return self._port

    def shutdown(self) -> None:
        self._running = False
        self.runner.stop()
        self.desktop.reset_physical_shield()
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        try:
            if PORT_FILE.exists():
                PORT_FILE.unlink()
        except OSError:
            pass
