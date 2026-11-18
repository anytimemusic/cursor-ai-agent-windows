from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import win32_backend as wb
from .desktop import DesktopController
from .packet_tracer import build_packet_tracer_topology, configure_packet_tracer_devices
from .perception import UiElement, match_elements

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) if sys.platform == "win32" else 0

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "see_screen",
            "description": (
                "Capture a screenshot with a 0..1000 coordinate grid, Set-of-Mark [1][2] numbered tags, and clickable ui_elements list (Windows UIA + OCR). "
                "Prefer click_element(tag_id/text) or 0..1000 grid coords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "monitor": {
                        "type": "string",
                        "description": "Target monitor: 'focused' (default), 'all', or monitor name like 'DISPLAY1' or 'DISPLAY2'.",
                    },
                    "som": {
                        "type": "boolean",
                        "description": "Include Set-of-Mark [id] tags and ui_elements list (default true).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_element",
            "description": (
                "Fast, high-precision click by Set-of-Mark [tag_id] or visible text/label (via UI Automation + OCR). "
                "Prefer this over guessing x,y whenever the target has a visible label or [id]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_id": {"type": "integer", "description": "Tag ID number [1..N] from ui_elements or screenshot."},
                    "text": {"type": "string", "description": "Visible label/text to locate and click if tag_id is omitted."},
                    "occurrence": {"type": "integer", "description": "1-based index when multiple elements match text (default 1)."},
                    "fresh": {"type": "boolean", "description": "Run fresh local OCR/UIA scan before matching text (default false)."},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "clicks": {"type": "integer", "description": "1 for single click, 2 for double click."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_text",
            "description": "Scan current monitor locally (UIA + OCR, <1s) to locate matching text/buttons in 0..1000 coords without needing a new vision turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search, or '*' to list detected UI elements."},
                    "monitor": {"type": "string", "description": "'focused' (default) or monitor name."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_macro",
            "description": (
                "Execute a fast sequence of GUI actions (click/move/drag/type/hotkey/wait) in ONE turn, "
                "optionally ending with a fresh annotated screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Ordered micro-actions (max 20).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": ["click", "move", "drag", "type", "hotkey", "wait"]},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "x1": {"type": "number"},
                                "y1": {"type": "number"},
                                "x2": {"type": "number"},
                                "y2": {"type": "number"},
                                "tag_id": {"type": "integer"},
                                "text": {"type": "string"},
                                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                                "clicks": {"type": "integer"},
                                "press_enter": {"type": "boolean"},
                                "keys": {"type": "string"},
                                "seconds": {"type": "number"},
                                "label": {"type": "string"},
                            },
                            "required": ["op"],
                        },
                    },
                    "verify_screen": {"type": "boolean", "description": "Capture annotated screenshot after macro (default true)."},
                    "monitor": {"type": "string", "description": "Target monitor for verify_screen ('focused' default)."},
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cursor_move",
            "description": "Visually glide the AI extra cursor to (x, y) on screen without clicking (great for pointing/highlighting).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "X coordinate (0..1000 normalized on monitor by default)"},
                    "y": {"type": "number", "description": "Y coordinate (0..1000 normalized on monitor by default)"},
                    "coord_mode": {
                        "type": "string",
                        "enum": ["normalized", "image", "global"],
                        "description": "'normalized' (0..1000 across the screenshot, DEFAULT), 'image' (raw image pixels), or 'global' (virtual desktop).",
                    },
                    "label": {"type": "string", "description": "Short label shown next to AI cursor."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cursor_click",
            "description": "Move the AI extra cursor to (x, y) and perform a real mouse click.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "X coordinate (0..1000 normalized by default)"},
                    "y": {"type": "number", "description": "Y coordinate (0..1000 normalized by default)"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (default 'left')."},
                    "clicks": {"type": "integer", "description": "1 for single click, 2 for double click."},
                    "coord_mode": {"type": "string", "enum": ["normalized", "image", "global"]},
                    "label": {"type": "string", "description": "Short status label shown on cursor."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cursor_drag",
            "description": "Drag with the AI cursor from (x1, y1) to (x2, y2).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "number"},
                    "y1": {"type": "number"},
                    "x2": {"type": "number"},
                    "y2": {"type": "number"},
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "coord_mode": {"type": "string", "enum": ["normalized", "image", "global"]},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cursor_scroll",
            "description": "Scroll at (x, y). Use dy > 0 to scroll down, dy < 0 to scroll up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "dy": {"type": "integer", "description": "Vertical scroll steps (+ down, - up)."},
                    "dx": {"type": "integer", "description": "Horizontal scroll steps."},
                    "coord_mode": {"type": "string", "enum": ["normalized", "image", "global"]},
                },
                "required": ["x", "y", "dy"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Type text into the currently focused window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type."},
                    "press_enter": {"type": "boolean", "description": "Press Enter after typing."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_hotkey",
            "description": "Press a key or keyboard shortcut, e.g. 'Return', 'Escape', 'ctrl+c', 'ctrl+l', 'ctrl+shift+t', 'alt+Tab', 'win+r'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "string"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a PowerShell command on Windows (or bash on Linux) for files, scripts, network/system checks. Use cursor_click/type_text for GUI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "PowerShell command to execute."},
                    "cwd": {"type": "string", "description": "Optional working directory."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file on disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and directories in a folder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Get or set the Windows clipboard text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "text": {"type": "string", "description": "Text to copy when action is 'set'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "window_action",
            "description": (
                "Inspect, launch, focus, resize, or close Windows desktop windows. "
                "Actions: 'clients', 'activewindow', 'monitors', 'focuswindow', 'exec', 'closewindow', 'minimize', 'maximize', 'restore'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "clients",
                            "activewindow",
                            "monitors",
                            "focuswindow",
                            "exec",
                            "closewindow",
                            "minimize",
                            "maximize",
                            "restore",
                        ],
                    },
                    "arg": {"type": "string", "description": "Argument, e.g. 'address:0x...', 'class:PacketTracer', 'title:Chrome', or executable name like 'notepad.exe'."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL or file with the default Windows application.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_seconds",
            "description": "Wait a short duration (e.g. while an app or webpage loads).",
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number"},
                },
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_clones",
            "description": (
                "Split the AI cursor into 2-4 parallel clone cursors (Swarm / Kage Bunshin) to execute independent sub-tasks concurrently, "
                "then automatically merge back into the main cursor when finished. Ideal for multi-monitor, multi-app, or mixed GUI + PowerShell tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tasks": {
                        "type": "array",
                        "description": "2 to 4 independent sub-tasks to run concurrently in parallel clone cursors.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Short 1-3 word label on clone cursor."},
                                "instruction": {"type": "string", "description": "Specific self-contained task for this clone cursor."},
                                "monitor": {"type": "string", "description": "Target monitor: 'focused' (default), 'DISPLAY1', or 'DISPLAY2'."},
                                "attach_screen": {"type": "boolean", "description": "Attach initial Set-of-Mark screenshot (default true; set false for pure shell/file tasks)."},
                            },
                            "required": ["title", "instruction"],
                        },
                    },
                },
                "required": ["tasks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "packet_tracer_build",
            "description": (
                "Build a Cisco Packet Tracer topology cleanly using dynamic screen calibration. "
                "Places devices cleanly onto canvas coordinates (0..1000), connects cables (supporting explicit from_port/to_port and parallel EtherChannel links), "
                "fast-forwards boot time, and returns an updated screenshot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clear_canvas": {
                        "type": "boolean",
                        "description": "Start a blank Packet Tracer canvas via Ctrl+N (auto-discards unsaved prompt) before placing devices. Set true when building a fresh lab or when older devices are still on the canvas; set false only when adding onto an already-open starter topology.",
                    },
                    "fast_forward": {
                        "type": "boolean",
                        "description": "Press Alt+D after building to fast-forward IOS boot/STP convergence (default true).",
                    },
                    "devices": {
                        "type": "array",
                        "description": "Devices to place on canvas.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Alias e.g. 'R1', 'S1', 'PC1', 'Server1'"},
                                "model": {"type": "string", "description": "PT model e.g. '4321', '4331', '1941', '2911', '2960-24TT', '3560-24PS', '2504', '3504', 'LAP-PT', 'PC-PT', 'Laptop-PT', 'Server-PT'"},
                                "x": {"type": "number", "description": "Normalized canvas X (100..900)"},
                                "y": {"type": "number", "description": "Normalized canvas Y (150..850)"},
                            },
                            "required": ["name", "model", "x", "y"],
                        },
                    },
                    "connections": {
                        "type": "array",
                        "description": "Cables between devices (e.g. from_port='G0/0/1', to_port='F0/5'). Supports multiple parallel links between the same switch pair.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                                "from_port": {"type": "string", "description": "Optional explicit source interface, e.g. 'G0/0/1', 'F0/5', 'F0/21', 'Fa0', 'G1'."},
                                "to_port": {"type": "string", "description": "Optional explicit target interface, e.g. 'F0/5', 'G0/1', 'F0/21'."},
                                "cable": {"type": "string", "enum": ["auto", "straight", "cross", "fiber", "serial", "console"]},
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "packet_tracer_configure",
            "description": (
                "Configure Cisco Packet Tracer devices cleanly in ONE batch call without manual UI clicking. "
                "Supports Switch/Router IOS CLI (STP, VLAN, Trunk, EtherChannel, Subinterface/RoaS, OSPF/RIP/Static, DHCP, SNMP) with auto-wait for boot & verification, "
                "plus optional canvas display_name rename, WLC Management IP, Server AAA/RADIUS & DHCP pools, Host IP/DHCP/Command Prompt (ping/ipconfig), "
                "or opening a Device Desktop GUI app ('browser', 'wireless') via open_app + keep_open=true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "devices": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string", "description": "Device label e.g. 'R1', 'Router0', 'S1', 'Switch0', 'PC1', 'PC0', 'WLC-1', 'RADIUS/SNMP Server'"},
                                "x": {"type": "number", "description": "Optional canvas X hint (0..1000)"},
                                "y": {"type": "number", "description": "Optional canvas Y hint (0..1000)"},
                                "display_name": {"type": "string", "description": "Optional new Display Name to set on canvas via Config -> Settings."},
                                "cli_commands": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "IOS CLI commands to paste for Router/Switch.",
                                },
                                "verify_cli": {
                                    "type": "boolean",
                                    "description": "Automatically append verification show commands after config (default true).",
                                },
                                "ipv4": {"type": "string"},
                                "mask": {"type": "string"},
                                "gateway": {"type": "string"},
                                "dns": {"type": "string"},
                                "dhcp": {"type": "boolean"},
                                "wlc_management": {"type": "object", "description": "Optional WLC Management settings {ipv4, mask, gateway, dns}."},
                                "radius_server": {"type": "object", "description": "Server AAA/RADIUS settings {enabled, clients:[{name,ip,secret}], users:[{username,password}]}."},
                                "dhcp_server": {"type": "object", "description": "Server DHCP pool(s) {enabled, pools:[{name,gateway,dns,start_ip,mask,max_users}]}."},
                                "terminal_commands": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Commands for PC/Laptop Command Prompt, e.g. ['ipconfig', 'ping 192.168.1.101']",
                                },
                                "open_app": {"type": "string", "description": "Optional panel/utility to open and keep open ('browser', 'wireless', 'services', 'config', 'cli', 'desktop')."},
                                "browser_url": {"type": "string", "description": "Optional URL when open_app='browser'."},
                                "keep_open": {"type": "boolean", "description": "Keep this device window open after configuring. Default false."},
                            },
                            "required": ["target"],
                        },
                    },
                },
                "required": ["devices"],
            },
        },
    },
]

CLONE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    s for s in TOOL_SCHEMAS if s.get("function", {}).get("name") != "spawn_clones"
]


@dataclass
class ToolResult:
    text: str
    image_data_url: str | None = None


class ToolExecutor:
    def __init__(
        self,
        desktop: DesktopController,
        config_getter: Any,
        stop_event: threading.Event,
        pause_gate: Any,
        spawn_clones_fn: Any = None,
    ) -> None:
        self.desktop = desktop
        self.get_config = config_getter
        self.stop_event = stop_event
        self.pause_gate = pause_gate
        self.spawn_clones_fn = spawn_clones_fn

    def _resolve_target_coords(
        self,
        args: dict[str, Any],
        monitor: str = "focused",
    ) -> tuple[int, int, UiElement | None, str]:
        tag_id = args.get("tag_id")
        text = str(args.get("text") or "").strip()
        occurrence = max(1, int(args.get("occurrence") or 1))
        fresh = bool(args.get("fresh", False))

        if tag_id is not None:
            tid = int(tag_id)
            el = next((e for e in self.desktop.last_elements if e.id == tid), None)
            if el is None:
                return 0, 0, None, f"Tag ID [{tid}] tidak ditemukan pada screenshot terakhir. Panggil see_screen/find_text."
            gx, gy = self.desktop.resolve_coords(el.x, el.y, "normalized")
            return gx, gy, el, ""

        if text:
            matches: list[UiElement] = []
            if not fresh and self.desktop.last_elements:
                matches = match_elements(self.desktop.last_elements, text)
            if not matches or fresh:
                self.desktop.emit({"type": "cursor_status", "label": f"Cari '{text[:18]}'..."})
                _, els = self.desktop.scan_ui(monitor=monitor)
                matches = match_elements(els, text)
            if not matches:
                sample = [f"[{e.id}] {e.text}" for e in self.desktop.last_elements[:12]]
                return 0, 0, None, f"Teks/elemen '{text}' tidak ditemukan. Contoh di layar: {', '.join(sample)}"
            idx = occurrence - 1
            if idx >= len(matches):
                return 0, 0, None, f"Hanya ada {len(matches)} hasil untuk '{text}' (diminta ke-{occurrence})."
            el = matches[idx]
            gx, gy = self.desktop.resolve_coords(el.x, el.y, "normalized")
            return gx, gy, el, ""

        gx, gy = self.desktop.resolve_coords(float(args["x"]), float(args["y"]), str(args.get("coord_mode", "normalized")))
        return gx, gy, None, ""

    def _sleep_cancellable(self, seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return False
            self.pause_gate()
            time.sleep(min(0.08, max(0.01, deadline - time.monotonic())))
        return not self.stop_event.is_set()

    def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        cfg = self.get_config()
        restore = bool(cfg.get("restore_cursor", True))
        glide_ms = int(cfg.get("glide_ms", 110))
        shot_w = int(cfg.get("screenshot_width", 1440))
        som_enabled = bool(cfg.get("som_enabled", True))

        if name == "see_screen":
            monitor = str(args.get("monitor") or "focused")
            use_som = som_enabled if args.get("som") is None else bool(args.get("som"))
            self.desktop.emit({"type": "cursor_status", "label": "Scan layar..." if use_som else "Liat layar..."})
            data_url, meta, elements = self.desktop.scan_and_capture(
                monitor=monitor,
                max_width=shot_w,
                draw_grid=True,
                draw_som=use_som,
            )
            info = self.desktop.build_screen_context(meta, elements if use_som else None)
            return ToolResult(text=json.dumps(info, ensure_ascii=False), image_data_url=data_url)

        if name == "find_text":
            query = str(args.get("query") or "").strip()
            monitor = str(args.get("monitor") or "focused")
            self.desktop.emit({"type": "cursor_status", "label": f"Cari '{query[:18]}'..."})
            meta, elements = self.desktop.scan_ui(monitor=monitor)
            hits = elements[:35] if query in ("", "*") else match_elements(elements, query)[:20]
            return ToolResult(
                text=json.dumps(
                    {
                        "monitor": meta.monitor,
                        "query": query,
                        "total_scanned": len(elements),
                        "matches": [e.to_compact_dict() for e in hits],
                    },
                    ensure_ascii=False,
                )
            )

        if name == "click_element":
            gx, gy, el, err = self._resolve_target_coords(args)
            if err:
                return ToolResult(text=json.dumps({"ok": False, "error": err}, ensure_ascii=False))
            assert el is not None
            res = self.desktop.click(
                gx,
                gy,
                button=str(args.get("button", "left")),
                clicks=int(args.get("clicks", 1)),
                restore_cursor=restore,
                glide_ms=glide_ms,
                label=el.text[:22],
            )
            return ToolResult(
                text=json.dumps({"ok": True, "matched": el.to_compact_dict(), **res}, ensure_ascii=False)
            )

        if name == "run_macro":
            steps = list(args.get("steps") or [])[:20]
            verify = bool(args.get("verify_screen", True))
            monitor = str(args.get("monitor") or "focused")
            step_glide = min(glide_ms, 85)
            results: list[dict[str, Any]] = []
            last_op = ""
            did_pointer_op = False
            has_pointer_ops = any(str(s.get("op") or "").lower().strip() in ("click", "drag", "scroll") for s in steps)
            orig_cursor = self.desktop.get_cursor_pos() if (restore and has_pointer_ops) else None
            shield_ctx = self.desktop.physical_cursor_shield() if has_pointer_ops else None

            if shield_ctx is not None:
                shield_ctx.__enter__()
            try:
                for idx, step in enumerate(steps, start=1):
                    self.pause_gate()
                    if self.stop_event.is_set():
                        results.append({"step": idx, "stopped": True})
                        break
                    op = str(step.get("op") or "").lower().strip()
                    last_op = op
                    if op == "click":
                        gx, gy, el, err = self._resolve_target_coords(step, monitor=monitor)
                        if err:
                            results.append({"step": idx, "op": op, "ok": False, "error": err})
                            break
                        lbl = str(step.get("label") or (el.text[:22] if el else "Klik"))
                        did_pointer_op = True
                        r = self.desktop.click(
                            gx,
                            gy,
                            button=str(step.get("button", "left")),
                            clicks=int(step.get("clicks", 1)),
                            restore_cursor=False,
                            glide_ms=step_glide,
                            label=lbl,
                        )
                        results.append({"step": idx, "op": op, "ok": True, **r})
                        self._sleep_cancellable(0.06)
                    elif op == "move":
                        gx, gy, el, err = self._resolve_target_coords(step, monitor=monitor)
                        if err:
                            results.append({"step": idx, "op": op, "ok": False, "error": err})
                            break
                        lbl = str(step.get("label") or (el.text[:22] if el else "Arahkan"))
                        x, y = self.desktop.move_ai_cursor(gx, gy, label=lbl, glide_ms=step_glide, move_physical=False)
                        results.append({"step": idx, "op": op, "ok": True, "x": x, "y": y})
                    elif op == "drag":
                        gx1, gy1 = self.desktop.resolve_coords(float(step["x1"]), float(step["y1"]), "normalized")
                        gx2, gy2 = self.desktop.resolve_coords(float(step["x2"]), float(step["y2"]), "normalized")
                        did_pointer_op = True
                        r = self.desktop.drag(
                            gx1,
                            gy1,
                            gx2,
                            gy2,
                            button=str(step.get("button", "left")),
                            restore_cursor=False,
                            glide_ms=step_glide,
                        )
                        results.append({"step": idx, "op": op, "ok": True, **r})
                        self._sleep_cancellable(0.06)
                    elif op == "type":
                        txt_val = step.get("text") if step.get("text") is not None else (step.get("keys") or "")
                        self.desktop.emit({"type": "cursor_status", "label": "Mengetik..."})
                        r = self.desktop.type_text(str(txt_val), press_enter=bool(step.get("press_enter", False)))
                        results.append({"step": idx, "op": op, "ok": True, **r})
                        self._sleep_cancellable(0.05)
                    elif op == "hotkey":
                        keys = str(step.get("keys") or "")
                        self.desktop.emit({"type": "cursor_status", "label": f"Shortcut {keys}"})
                        r = self.desktop.press_hotkey(keys)
                        results.append({"step": idx, "op": op, "ok": True, **r})
                        self._sleep_cancellable(0.06)
                    elif op == "wait":
                        sec = max(0.05, min(10.0, float(step.get("seconds", 0.3))))
                        self.desktop.emit({"type": "cursor_status", "label": f"Tunggu {sec:.1f}s..."})
                        ok_w = self._sleep_cancellable(sec)
                        results.append({"step": idx, "op": op, "ok": ok_w, "waited": sec})
                        if not ok_w:
                            break
                    else:
                        results.append({"step": idx, "op": op, "ok": False, "error": f"Unknown macro op: {op}"})
                        break
            finally:
                try:
                    if self.stop_event.is_set() and did_pointer_op:
                        self.desktop.disarm_if_packet_tracer()
                    if restore and did_pointer_op and orig_cursor is not None:
                        try:
                            self.desktop._restore_physical(*orig_cursor)
                        except Exception:
                            pass
                finally:
                    if shield_ctx is not None:
                        shield_ctx.__exit__(None, None, None)

            payload: dict[str, Any] = {"ok": all(r.get("ok", False) for r in results), "steps_run": results}
            if verify and not self.stop_event.is_set():
                if last_op != "wait":
                    self._sleep_cancellable(0.12)
                data_url, meta, elements = self.desktop.scan_and_capture(
                    monitor=monitor,
                    max_width=shot_w,
                    draw_grid=True,
                    draw_som=som_enabled,
                )
                payload["screen"] = self.desktop.build_screen_context(meta, elements if som_enabled else None)
                return ToolResult(text=json.dumps(payload, ensure_ascii=False), image_data_url=data_url)
            return ToolResult(text=json.dumps(payload, ensure_ascii=False))

        if name == "cursor_move":
            gx, gy = self.desktop.resolve_coords(float(args["x"]), float(args["y"]), str(args.get("coord_mode", "normalized")))
            label = str(args.get("label") or "Menunjuk")
            x, y = self.desktop.move_ai_cursor(gx, gy, label=label, glide_ms=glide_ms, move_physical=False)
            return ToolResult(text=json.dumps({"ok": True, "global_x": x, "global_y": y}))

        if name == "cursor_click":
            gx, gy = self.desktop.resolve_coords(float(args["x"]), float(args["y"]), str(args.get("coord_mode", "normalized")))
            res = self.desktop.click(
                gx,
                gy,
                button=str(args.get("button", "left")),
                clicks=int(args.get("clicks", 1)),
                restore_cursor=restore,
                glide_ms=glide_ms,
                label=str(args.get("label") or "Klik"),
            )
            return ToolResult(text=json.dumps({"ok": True, **res}))

        if name == "cursor_drag":
            cm = str(args.get("coord_mode", "normalized"))
            gx1, gy1 = self.desktop.resolve_coords(float(args["x1"]), float(args["y1"]), cm)
            gx2, gy2 = self.desktop.resolve_coords(float(args["x2"]), float(args["y2"]), cm)
            res = self.desktop.drag(
                gx1,
                gy1,
                gx2,
                gy2,
                button=str(args.get("button", "left")),
                restore_cursor=restore,
                glide_ms=glide_ms,
            )
            return ToolResult(text=json.dumps({"ok": True, **res}))

        if name == "cursor_scroll":
            gx, gy = self.desktop.resolve_coords(float(args["x"]), float(args["y"]), str(args.get("coord_mode", "normalized")))
            res = self.desktop.scroll(
                gx,
                gy,
                dy=int(args.get("dy", 0)),
                dx=int(args.get("dx", 0)),
                restore_cursor=restore,
                glide_ms=min(180, glide_ms),
            )
            return ToolResult(text=json.dumps({"ok": True, **res}))

        if name == "type_text":
            self.desktop.emit({"type": "cursor_status", "label": "Mengetik..."})
            res = self.desktop.type_text(str(args["text"]), press_enter=bool(args.get("press_enter", False)))
            res["active_window"] = self.desktop.get_active_window()
            return ToolResult(text=json.dumps({"ok": True, **res}, ensure_ascii=False))

        if name == "press_hotkey":
            keys = str(args["keys"])
            self.desktop.emit({"type": "cursor_status", "label": f"Shortcut {keys}"})
            res = self.desktop.press_hotkey(keys)
            res["active_window"] = self.desktop.get_active_window()
            return ToolResult(text=json.dumps({"ok": True, **res}, ensure_ascii=False))

        if name in ("run_shell", "run_bash"):
            command = str(args["command"])
            cwd = os.path.expanduser(str(args.get("cwd") or Path.home()))
            timeout = max(1, min(120, int(args.get("timeout", 30))))
            self.desktop.emit({"type": "cursor_status", "label": "Jalanin command..."})
            return ToolResult(text=self._run_cancellable_shell(command, cwd, timeout))

        if name == "read_file":
            p = Path(os.path.expanduser(str(args["path"])))
            if not p.exists():
                return ToolResult(text=json.dumps({"error": f"File not found: {p}"}))
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(1, int(args.get("start_line") or 1))
            end = min(len(lines), int(args.get("end_line") or min(len(lines), start + 249)))
            snippet = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
            return ToolResult(text=json.dumps({"path": str(p), "total_lines": len(lines), "start": start, "end": end, "content": snippet[:32000]}, ensure_ascii=False))

        if name == "write_file":
            p = Path(os.path.expanduser(str(args["path"])))
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(args["content"])
            p.write_text(content, encoding="utf-8")
            return ToolResult(text=json.dumps({"ok": True, "path": str(p), "bytes": len(content.encode("utf-8"))}))

        if name == "list_dir":
            p = Path(os.path.expanduser(str(args.get("path") or ".")))
            if not p.exists():
                return ToolResult(text=json.dumps({"error": f"Directory not found: {p}"}))
            entries = []
            for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:150]:
                entries.append({"name": item.name, "type": "dir" if item.is_dir() else "file"})
            return ToolResult(text=json.dumps({"path": str(p.resolve()), "entries": entries}, ensure_ascii=False))

        if name == "clipboard":
            act = str(args["action"])
            if act == "get":
                ok_g, out, err = wb.clipboard_get()
                return ToolResult(text=json.dumps({"ok": ok_g, "text": out[:16000], "error": err}))
            if act == "set":
                txt = str(args.get("text") or "")
                ok_cp, err = wb.clipboard_set(txt)
                return ToolResult(text=json.dumps({"ok": ok_cp, "copied_chars": len(txt), "error": err}))
            return ToolResult(text=json.dumps({"error": f"Unknown clipboard action: {act}"}))

        if name in ("window_action", "hyprland_action"):
            act = str(args["action"])
            arg = str(args.get("arg") or "")
            if act == "clients":
                return ToolResult(text=json.dumps({"clients": self.desktop.get_windows()}, ensure_ascii=False))
            if act == "activewindow":
                return ToolResult(text=json.dumps(self.desktop.get_active_window(), ensure_ascii=False))
            if act == "monitors":
                return ToolResult(text=json.dumps({"monitors": self.desktop.get_monitors()}, ensure_ascii=False))
            if act == "exec":
                wb.launch_shell_or_app(arg)
                return ToolResult(text=json.dumps({"ok": True, "launched": arg}))
            if act == "focuswindow":
                ok = wb.focus_window(arg)
                aw = self.desktop.get_active_window()
                if ok and aw.get("at") and aw.get("size"):
                    wx, wy = aw["at"]
                    ww, wh = aw["size"]
                    cx, cy = self.desktop.ai_cursor_x, self.desktop.ai_cursor_y
                    if not (wx <= cx <= wx + ww and wy <= cy <= wy + wh):
                        self.desktop.move_ai_cursor(wx + ww // 2, wy + wh // 2, label=aw.get("class") or "Fokus", glide_ms=glide_ms, move_physical=False)
                return ToolResult(text=json.dumps({"ok": ok, "active_window": aw}, ensure_ascii=False))
            if act == "closewindow":
                ok = wb.close_window(arg)
                return ToolResult(text=json.dumps({"ok": ok}))
            if act in ("minimize", "maximize", "restore"):
                ok = wb.show_window_state(arg, act)
                return ToolResult(text=json.dumps({"ok": ok, "active_window": self.desktop.get_active_window()}, ensure_ascii=False))
            return ToolResult(text=json.dumps({"error": f"Unsupported window action: {act}"}))

        if name == "open_url":
            url = str(args["url"])
            self.desktop.emit({"type": "cursor_status", "label": "Buka link..."})
            if sys.platform == "win32":
                os.startfile(url)  # type: ignore[attr-defined]
            else:
                webbrowser.open(url)
            time.sleep(0.7)
            aw = self.desktop.get_active_window()
            if aw.get("at") and aw.get("size"):
                wx, wy = aw["at"]
                ww, wh = aw["size"]
                self.desktop.move_ai_cursor(wx + ww // 2, wy + wh // 2, label="Membuka...", glide_ms=glide_ms, move_physical=False)
            return ToolResult(text=json.dumps({"ok": True, "opened": url, "active_window": aw}, ensure_ascii=False))

        if name == "wait_seconds":
            sec = max(0.1, min(20.0, float(args.get("seconds", 1.0))))
            self.desktop.emit({"type": "cursor_status", "label": f"Tunggu {sec:.1f}s..."})
            deadline = time.monotonic() + sec
            while time.monotonic() < deadline:
                if self.stop_event.is_set():
                    return ToolResult(text=json.dumps({"stopped": True}))
                self.pause_gate()
                time.sleep(0.1)
            return ToolResult(text=json.dumps({"ok": True, "waited": sec}))

        if name == "spawn_clones":
            if self.spawn_clones_fn is None:
                return ToolResult(text=json.dumps({"ok": False, "error": "Clone swarm tidak tersedia di konteks sub-klon."}))
            tasks = list(args.get("tasks") or [])
            if not tasks:
                return ToolResult(text=json.dumps({"ok": False, "error": "Parameter 'tasks' tidak boleh kosong."}))
            res = self.spawn_clones_fn(tasks[:4])
            all_ok = all(bool(r.get("ok", False)) for r in res)
            return ToolResult(text=json.dumps({"ok": all_ok, "clones_ran": len(res), "results": res}, ensure_ascii=False))

        if name == "packet_tracer_build":
            devs = list(args.get("devices") or [])
            conns = list(args.get("connections") or [])
            if not devs and not conns:
                return ToolResult(text=json.dumps({"ok": False, "error": "Parameter 'devices' atau 'connections' tidak boleh kosong."}))
            self.desktop.emit({"type": "cursor_status", "label": "Packet Tracer..."})
            res = build_packet_tracer_topology(
                self.desktop,
                devices=devs,
                connections=conns,
                stop_event=self.stop_event,
                pause_gate=self.pause_gate,
                glide_ms=glide_ms,
                restore_cursor=restore,
                clear_canvas=bool(args.get("clear_canvas", False)),
                fast_forward=bool(args.get("fast_forward", True)),
            )
            data_url, meta, elements = self.desktop.scan_and_capture(
                monitor=res["monitor"],
                max_width=shot_w,
                draw_grid=True,
                draw_som=som_enabled,
            )
            res["screen"] = self.desktop.build_screen_context(meta, elements if som_enabled else None)
            return ToolResult(text=json.dumps(res, ensure_ascii=False), image_data_url=data_url)

        if name == "packet_tracer_configure":
            devs = list(args.get("devices") or [])
            if not devs:
                return ToolResult(text=json.dumps({"ok": False, "error": "Parameter 'devices' tidak boleh kosong."}))
            self.desktop.emit({"type": "cursor_status", "label": "Config PT..."})
            res = configure_packet_tracer_devices(
                self.desktop,
                devices=devs,
                stop_event=self.stop_event,
                pause_gate=self.pause_gate,
                glide_ms=glide_ms,
                restore_cursor=restore,
            )
            data_url, meta, elements = self.desktop.scan_and_capture(
                monitor=res["monitor"],
                max_width=shot_w,
                draw_grid=True,
                draw_som=som_enabled,
            )
            res["screen"] = self.desktop.build_screen_context(meta, elements if som_enabled else None)
            return ToolResult(text=json.dumps(res, ensure_ascii=False), image_data_url=data_url)

        return ToolResult(text=json.dumps({"error": f"Unknown tool: {name}"}))

    def _run_cancellable_shell(self, command: str, cwd: str, timeout: int) -> str:
        work_dir = cwd if os.path.isdir(cwd) else None
        if sys.platform == "win32":
            proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", command],
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
            )
        else:
            proc = subprocess.Popen(
                ["bash", "-lc", command],
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if self.stop_event.is_set() or time.monotonic() > deadline:
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=_CREATE_NO_WINDOW,
                            timeout=3.0,
                        )
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                        time.sleep(0.2)
                        if proc.poll() is None:
                            os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                reason = "stopped_by_user" if self.stop_event.is_set() else "timeout"
                return json.dumps({"exit_code": -1, "error": reason})
            time.sleep(0.08)

        out, err = proc.communicate()
        return json.dumps(
            {
                "exit_code": proc.returncode,
                "stdout": (out or "")[:16000],
                "stderr": (err or "")[:8000],
            },
            ensure_ascii=False,
        )
