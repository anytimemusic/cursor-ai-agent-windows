from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from PIL import Image

IS_WINDOWS = sys.platform == "win32"

# Virtual desktop metrics
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Mouse & Keyboard SendInput constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

# Window style & DWM constants
GWL_STYLE = -16
GWL_EXSTYLE = -20
GW_OWNER = 4
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
WM_CLOSE = 0x0010

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


VK_MAP: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "lctrl": 0xA2,
    "rctrl": 0xA3,
    "alt": 0x12,
    "menu": 0x12,
    "shift": 0x10,
    "lshift": 0xA0,
    "rshift": 0xA1,
    "win": 0x5B,
    "super": 0x5B,
    "meta": 0x5B,
    "logo": 0x5B,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "prior": 0x21,
    "pagedown": 0x22,
    "next": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "capslock": 0x14,
    "printscreen": 0x2C,
    "-": 0xBD,
    "minus": 0xBD,
    "=": 0xBB,
    "equal": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
}
for _i in range(1, 13):
    VK_MAP[f"f{_i}"] = 0x6F + _i
for _c in "0123456789":
    VK_MAP[_c] = ord(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK_MAP[_c] = ord(_c.upper())

EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0xA3, 0xA5,
}


def to_sendinput_abs(gx: int, gy: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """Convert global physical pixel coordinates (gx, gy) into Win32 0..65535 VirtualDesk normalized units."""
    w = max(1, int(vw))
    h = max(1, int(vh))
    nx = int(((int(gx) - int(vx)) + 0.5) * 65536.0 / w)
    ny = int(((int(gy) - int(vy)) + 0.5) * 65536.0 / h)
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def parse_hotkey_tokens(keys: str) -> tuple[list[int], list[int]]:
    """Parse 'ctrl+shift+6', 'alt+d', or 'Return' into (modifier_vks, key_vks)."""
    parts = [p.strip() for p in (keys or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("Empty hotkey string")
    mod_names = {"ctrl", "control", "lctrl", "rctrl", "alt", "menu", "shift", "lshift", "rshift", "win", "super", "meta", "logo"}
    mods: list[int] = []
    main_keys: list[int] = []
    for p in parts:
        low = p.lower()
        vk = VK_MAP.get(low)
        if vk is None and len(p) == 1:
            vk = VK_MAP.get(p.lower())
        if vk is None:
            raise ValueError(f"Unsupported hotkey token: {p!r}")
        if low in mod_names:
            mods.append(vk)
        else:
            main_keys.append(vk)
    return mods, main_keys


def find_tesseract() -> str | None:
    """Locate tesseract.exe on PATH or standard Windows install directories."""
    p = shutil.which("tesseract")
    if p:
        return p
    if IS_WINDOWS:
        candidates = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        ]
        for cand in candidates:
            if cand and cand.is_file():
                return str(cand)
    return None


def normalize_win_process_name(proc_name: str) -> str:
    """Map Windows executable names to normalized app class tags matching Linux tool hints."""
    low = (proc_name or "").strip().lower()
    if low.endswith(".exe"):
        low = low[:-4]
    if "packettracer" in low:
        return "PacketTracer"
    return low or "unknown"


if IS_WINDOWS:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    dwmapi = ctypes.windll.dwmapi
    try:
        shcore = ctypes.windll.shcore
    except Exception:
        shcore = None

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [
            ("type", wintypes.DWORD),
            ("union", _INPUT_UNION),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def enable_dpi_awareness() -> None:
    """Enable Per-Monitor DPI Awareness V2 before creating Qt/mss objects so all coords stay 1:1 physical px."""
    if not IS_WINDOWS:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        if shcore is not None:
            shcore.SetProcessDpiAwareness(2)
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def get_virtual_bounds() -> tuple[int, int, int, int]:
    if not IS_WINDOWS:
        return 0, 0, 1920, 1080
    vx = int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    vy = int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    vw = max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)))
    vh = max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)))
    return vx, vy, vw, vh


def get_cursor_pos() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 0, 0
    pt = wintypes.POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return int(pt.x), int(pt.y)
    return 0, 0


def _send_inputs(items: list[Any]) -> None:
    if not IS_WINDOWS or not items:
        return
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


def move_pointer(gx: int, gy: int) -> None:
    if not IS_WINDOWS:
        return
    vx, vy, vw, vh = get_virtual_bounds()
    ax, ay = to_sendinput_abs(gx, gy, vx, vy, vw, vh)
    inp = INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=ax,
            dy=ay,
            mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            time=0,
            dwExtraInfo=0,
        ),
    )
    _send_inputs([inp])


def click_pointer(gx: int, gy: int, button: str = "left", clicks: int = 1) -> None:
    if not IS_WINDOWS:
        return
    move_pointer(gx, gy)
    time.sleep(0.035)
    down_f, up_f = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))

    vx, vy, vw, vh = get_virtual_bounds()
    ax, ay = to_sendinput_abs(gx, gy, vx, vy, vw, vh)
    cnt = max(1, min(3, int(clicks)))
    for i in range(cnt):
        d = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=down_f | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time=0, dwExtraInfo=0))
        u = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax, dy=ay, mouseData=0, dwFlags=up_f | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time=0, dwExtraInfo=0))
        _send_inputs([d])
        time.sleep(0.045)
        _send_inputs([u])
        if i + 1 < cnt:
            time.sleep(0.065)
    time.sleep(0.025)


def drag_pointer(gx1: int, gy1: int, gx2: int, gy2: int, button: str = "left") -> None:
    if not IS_WINDOWS:
        return
    down_f, up_f = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(button, (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    vx, vy, vw, vh = get_virtual_bounds()
    ax1, ay1 = to_sendinput_abs(gx1, gy1, vx, vy, vw, vh)
    move_pointer(gx1, gy1)
    time.sleep(0.04)
    _send_inputs([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax1, dy=ay1, mouseData=0, dwFlags=down_f | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time=0, dwExtraInfo=0))])
    time.sleep(0.05)
    steps = 20
    for i in range(1, steps + 1):
        cx = round(gx1 + (gx2 - gx1) * (i / steps))
        cy = round(gy1 + (gy2 - gy1) * (i / steps))
        move_pointer(cx, cy)
        time.sleep(0.01)
    ax2, ay2 = to_sendinput_abs(gx2, gy2, vx, vy, vw, vh)
    time.sleep(0.04)
    _send_inputs([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=ax2, dy=ay2, mouseData=0, dwFlags=up_f | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, time=0, dwExtraInfo=0))])
    time.sleep(0.03)


def scroll_pointer(gx: int, gy: int, dy: int, dx: int = 0) -> None:
    if not IS_WINDOWS:
        return
    move_pointer(gx, gy)
    time.sleep(0.02)
    steps = max(1, min(15, abs(int(dy)) if dy != 0 else abs(int(dx))))
    # On Windows positive WHEEL_DELTA scrolls UP, negative scrolls DOWN (opposite of dy convention in tools)
    v_step = (-WHEEL_DELTA if dy > 0 else (WHEEL_DELTA if dy < 0 else 0))
    h_step = (WHEEL_DELTA if dx > 0 else (-WHEEL_DELTA if dx < 0 else 0))
    for _ in range(steps):
        ev: list[Any] = []
        if v_step:
            ev.append(INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=v_step & 0xFFFFFFFF, dwFlags=MOUSEEVENTF_WHEEL, time=0, dwExtraInfo=0)))
        if h_step:
            ev.append(INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx=0, dy=0, mouseData=h_step & 0xFFFFFFFF, dwFlags=MOUSEEVENTF_HWHEEL, time=0, dwExtraInfo=0)))
        if ev:
            _send_inputs(ev)
        time.sleep(0.03)


def send_unicode_text(text: str, delay_ms: int = 4) -> None:
    if not IS_WINDOWS or not text:
        return
    for ch in text:
        if ch == "\r":
            continue
        if ch == "\n":
            send_vk_hotkey("Return")
            time.sleep(0.02)
            continue
        code = ord(ch)
        inps = [
            INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0)),
            INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=0)),
        ]
        _send_inputs(inps)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)


def send_vk_hotkey(keys: str) -> None:
    if not IS_WINDOWS:
        return
    mods, main_keys = parse_hotkey_tokens(keys)
    seq: list[Any] = []

    def _kd(vk: int, up: bool = False) -> Any:
        fl = KEYEVENTF_KEYUP if up else 0
        if vk in EXTENDED_VKS:
            fl |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=fl, time=0, dwExtraInfo=0))

    for m in mods:
        seq.append(_kd(m, up=False))
    for k in main_keys:
        seq.append(_kd(k, up=False))
        seq.append(_kd(k, up=True))
    for m in reversed(mods):
        seq.append(_kd(m, up=True))
    _send_inputs(seq)


def clipboard_get() -> tuple[bool, str, str]:
    if not IS_WINDOWS:
        return False, "", "Not running on Windows"
    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if not h:
                    return True, "", ""
                ptr = kernel32.GlobalLock(h)
                if not ptr:
                    return True, "", ""
                try:
                    return True, ctypes.wstring_at(ptr), ""
                finally:
                    kernel32.GlobalUnlock(h)
            except Exception as e:
                return False, "", str(e)
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False, "", "OpenClipboard failed"


def clipboard_set(text: str) -> tuple[bool, str]:
    if not IS_WINDOWS:
        return False, "Not running on Windows"
    data = (str(text) + "\0").encode("utf-16-le")
    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                user32.EmptyClipboard()
                hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not hmem:
                    return False, "GlobalAlloc failed"
                ptr = kernel32.GlobalLock(hmem)
                if not ptr:
                    kernel32.GlobalFree(hmem)
                    return False, "GlobalLock failed"
                ctypes.memmove(ptr, data, len(data))
                kernel32.GlobalUnlock(hmem)
                if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
                    kernel32.GlobalFree(hmem)
                    return False, "SetClipboardData failed"
                return True, ""
            except Exception as e:
                return False, str(e)
            finally:
                user32.CloseClipboard()
        time.sleep(0.02)
    return False, "OpenClipboard failed"


def get_monitors() -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        return [{"id": 0, "name": "DISPLAY1", "x": 0, "y": 0, "logical_w": 1920, "logical_h": 1080, "pixel_w": 1920, "pixel_h": 1080, "scale": 1.0, "dpi_scale": 1.0, "focused": True, "workspace": "1", "work_bottom": 1040}]

    fg = user32.GetForegroundWindow()
    fg_mon = user32.MonitorFromWindow(fg, 2) if fg else None
    if not fg_mon:
        pt = wintypes.POINT(*get_cursor_pos())
        fg_mon = user32.MonitorFromPoint(pt, 2)

    monitors: list[dict[str, Any]] = []

    def _cb(hmon: Any, _hdc: Any, _lprect: Any, _lparam: Any) -> bool:
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return True
        r = mi.rcMonitor
        w_rect = mi.rcWork
        pw = max(1, int(r.right - r.left))
        ph = max(1, int(r.bottom - r.top))
        dpi_scale = 1.0
        if shcore is not None:
            try:
                pct = ctypes.c_uint(100)
                if shcore.GetScaleFactorForMonitor(hmon, ctypes.byref(pct)) == 0 and pct.value >= 100:
                    dpi_scale = pct.value / 100.0
            except Exception:
                pass
        idx = len(monitors)
        raw_dev = str(mi.szDevice or f"DISPLAY{idx + 1}")
        clean_name = raw_dev.replace("\\\\.\\", "")
        monitors.append(
            {
                "id": idx,
                "name": clean_name,
                "device_name": raw_dev,
                "x": int(r.left),
                "y": int(r.top),
                # Under DPI-Aware V2 all Win32 screen coordinates & SendInput stay 1:1 physical pixels
                "logical_w": pw,
                "logical_h": ph,
                "pixel_w": pw,
                "pixel_h": ph,
                "scale": 1.0,
                "dpi_scale": dpi_scale,
                "focused": bool(fg_mon == hmon),
                "workspace": str(idx + 1),
                "work_left": int(w_rect.left),
                "work_top": int(w_rect.top),
                "work_right": int(w_rect.right),
                "work_bottom": int(w_rect.bottom),
            }
        )
        return True

    user32.EnumDisplayMonitors(0, 0, _MONITORENUMPROC(_cb), 0)
    if monitors and not any(m["focused"] for m in monitors):
        monitors[0]["focused"] = True
    return monitors


def _hwnd_process_name(hwnd: int) -> tuple[int, str]:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return 0, ""
    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not hproc:
        return int(pid.value), ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        sz = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
            return int(pid.value), Path(buf.value).name
    finally:
        kernel32.CloseHandle(hproc)
    return int(pid.value), ""


def _hwnd_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    try:
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:
            return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)
    except Exception:
        pass
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return int(rect.left), int(rect.top), int(rect.right - rect.left), int(rect.bottom - rect.top)


def _describe_hwnd(hwnd: int, own_pid: int | None = None, mons: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if not IS_WINDOWS or not hwnd:
        return None
    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
        return None
    cloaked = ctypes.c_int(0)
    try:
        if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
            return None
    except Exception:
        pass

    pid, pname = _hwnd_process_name(hwnd)
    if own_pid is not None and pid == own_pid:
        return None

    x, y, w, h = _hwnd_rect(hwnd)
    if w <= 2 or h <= 2:
        return None

    tbuf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, tbuf, 512)
    title = tbuf.value.strip()

    cbuf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, cbuf, 256)
    win_cls = cbuf.value.strip()

    # Ignore shell desktop/taskbar helper surfaces without titles
    if win_cls in ("Progman", "WorkerW", "Windows.UI.Core.CoreWindow") and not title:
        return None

    style = ctypes.c_ulong(user32.GetWindowLongW(hwnd, GWL_STYLE)).value
    exstyle = ctypes.c_ulong(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
    is_popup = bool(style & WS_POPUP)
    has_caption = bool((style & WS_CAPTION) == WS_CAPTION)
    app_cls = normalize_win_process_name(pname or win_cls)

    if app_cls == "PacketTracer":
        tlow = title.lower()
        # Distinguish main PT workspace and tiled-like large device windows (Switch0/Router0/PC0) from floating QMenus/tooltips/modals
        if tlow.startswith("cisco packet tracer") and not is_popup and w >= 480 and h >= 360:
            floating = False
        elif has_caption and not is_popup and w >= 420 and h >= 320 and "-- cisco packet tracer" not in tlow:
            floating = False
        else:
            floating = True
    else:
        floating = bool(is_popup or (exstyle & WS_EX_TOOLWINDOW) or user32.GetWindow(hwnd, GW_OWNER))

    mon_id = 0
    ws = "1"
    if mons:
        cx, cy = x + w // 2, y + h // 2
        m_hit = next((m for m in mons if m["x"] <= cx < m["x"] + m["logical_w"] and m["y"] <= cy < m["y"] + m["logical_h"]), mons[0])
        mon_id = int(m_hit.get("id", 0))
        ws = str(m_hit.get("workspace", "1"))

    return {
        "address": hex(hwnd),
        "hwnd": int(hwnd),
        "class": app_cls,
        "win_class": win_cls,
        "title": title[:120],
        "at": [x, y],
        "size": [w, h],
        "workspace": ws,
        "monitor": mon_id,
        "visible_now": True,
        "floating": floating,
        "xwayland": False,
    }


def get_windows(skip_own_pid: bool = True) -> list[dict[str, Any]]:
    if not IS_WINDOWS:
        return []
    own_pid = os.getpid() if skip_own_pid else None
    mons = get_monitors()
    wins: list[dict[str, Any]] = []

    def _cb(hwnd: int, _lparam: int) -> bool:
        info = _describe_hwnd(hwnd, own_pid=own_pid, mons=mons)
        if info is not None:
            wins.append(info)
        return True

    user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return wins


def get_active_window() -> dict[str, Any]:
    if not IS_WINDOWS:
        return {}
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {}
    return _describe_hwnd(hwnd, own_pid=None, mons=get_monitors()) or {}


def resolve_window_hwnd(target: str) -> int | None:
    if not IS_WINDOWS:
        return None
    s = (target or "").strip()
    if not s:
        return int(user32.GetForegroundWindow() or 0) or None
    low = s.lower()
    if low.startswith("address:") or low.startswith("hwnd:") or low.startswith("0x"):
        raw = s.split(":", 1)[-1].strip()
        try:
            return int(raw, 16) if raw.startswith("0x") else int(raw)
        except ValueError:
            return None
    wins = get_windows()
    if low.startswith("class:"):
        q = low.split(":", 1)[1].strip()
        hit = next((w for w in wins if q in w["class"].lower() or q in str(w.get("win_class", "")).lower()), None)
        return hit["hwnd"] if hit else None
    if low.startswith("title:"):
        q = low.split(":", 1)[1].strip()
        hit = next((w for w in wins if q in w["title"].lower()), None)
        return hit["hwnd"] if hit else None
    hit = next((w for w in wins if low in w["title"].lower() or low in w["class"].lower()), None)
    return hit["hwnd"] if hit else None


def focus_window(target: str) -> bool:
    if not IS_WINDOWS:
        return False
    hwnd = resolve_window_hwnd(target)
    if not hwnd:
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tg_tid = user32.GetWindowThreadProcessId(hwnd, None)
        if fg_tid and fg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, fg_tid, True)
        if tg_tid and tg_tid != cur_tid:
            user32.AttachThreadInput(cur_tid, tg_tid, True)
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if fg_tid and fg_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            if tg_tid and tg_tid != cur_tid:
                user32.AttachThreadInput(cur_tid, tg_tid, False)
        return True
    except Exception:
        return False


def close_window(target: str) -> bool:
    if not IS_WINDOWS:
        return False
    hwnd = resolve_window_hwnd(target)
    if not hwnd:
        return False
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def show_window_state(target: str, mode: str) -> bool:
    if not IS_WINDOWS:
        return False
    hwnd = resolve_window_hwnd(target)
    if not hwnd:
        return False
    cmd = {"minimize": SW_MINIMIZE, "maximize": SW_MAXIMIZE, "restore": SW_RESTORE}.get(mode, SW_RESTORE)
    return bool(user32.ShowWindow(hwnd, cmd))


def capture_region_mss(x: int, y: int, w: int, h: int) -> Image.Image:
    import mss

    with mss.mss() as sct:
        shot = sct.grab({"left": int(x), "top": int(y), "width": max(1, int(w)), "height": max(1, int(h))})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def launch_shell_or_app(command: str) -> None:
    if IS_WINDOWS:
        subprocess.Popen(
            command,
            shell=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(["bash", "-lc", command], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
