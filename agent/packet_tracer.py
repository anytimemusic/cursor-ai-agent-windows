from __future__ import annotations

import io
import math
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from . import win32_backend as wb
from .desktop import DesktopController
from .perception import _collect_ocr_elements, match_elements

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

# Cache recently placed device canvas-normalized coordinates (0..1000) across turns
_RECENT_PT_NORM: dict[str, tuple[float, float]] = {}

# Known instant category/subcategory/slot mappings relative to Packet Tracer's toolbar
KNOWN_DEVICE_SLOTS: dict[str, tuple[str, int, int]] = {
    "pc": ("end", 1, 1),
    "pc-pt": ("end", 1, 1),
    "laptop": ("end", 1, 2),
    "laptop-pt": ("end", 1, 2),
    "server": ("end", 1, 3),
    "server-pt": ("end", 1, 3),
    "printer": ("end", 1, 5),
    "printer-pt": ("end", 1, 5),
    "ip-phone": ("end", 1, 6),
    "4331": ("network", 1, 2),
    "isr4331": ("network", 1, 2),
    "4321": ("network", 1, 3),
    "isr4321": ("network", 1, 3),
    "1941": ("network", 1, 5),
    "2901": ("network", 1, 6),
    "2911": ("network", 1, 7),
    "router": ("network", 1, 7),
    "2960": ("network", 2, 1),
    "2960-24tt": ("network", 2, 1),
    "switch": ("network", 2, 1),
    "pt-switch": ("network", 2, 2),
    "3560": ("network", 2, 4),
    "3560-24ps": ("network", 2, 4),
    "3650": ("network", 2, 5),
    "3650-24ps": ("network", 2, 5),
    "hub": ("network", 3, 1),
    "pt-hub": ("network", 3, 1),
    "lap": ("network", 4, 2),
    "lap-pt": ("network", 4, 2),
    "lightweight-ap": ("network", 4, 2),
    "3702i": ("network", 4, 3),
    "air-cap3702i": ("network", 4, 3),
    "wlc": ("network", 4, 4),
    "wlc-pt": ("network", 4, 4),
    "3504": ("network", 4, 5),
    "wlc-3504": ("network", 4, 5),
    "2504": ("network", 4, 6),
    "wlc-2504": ("network", 4, 6),
}

CABLE_SLOTS: dict[str, int] = {
    "auto": 1,
    "automatic": 1,
    "console": 2,
    "straight": 3,
    "copper-straight": 3,
    "cross": 4,
    "crossover": 4,
    "copper-cross": 4,
    "fiber": 5,
    "phone": 6,
    "coaxial": 7,
    "serial": 8,
    "serial-dce": 8,
    "serial-dte": 9,
}

CATEGORY_X_OFFSET: dict[str, int] = {
    "network": 19,
    "end": 56,
    "components": 91,
    "connections": 126,
}


@dataclass
class PTCalibration:
    monitor: str
    window_address: str
    win_left_x: int
    win_top_y: int
    win_w: int
    win_h: int
    nav_bottom_y: int
    cat_y: int
    subcat_y: int
    search_x: int
    search_y: int
    canvas_x1: int
    canvas_y1: int
    canvas_x2: int
    canvas_y2: int
    work_bottom: int = 0

    def category_xy(self, cat: str) -> tuple[int, int]:
        off = CATEGORY_X_OFFSET.get(cat.lower(), 19)
        return self.win_left_x + off, self.cat_y

    def subcat_xy(self, idx: int) -> tuple[int, int]:
        n = max(1, min(8, int(idx)))
        return self.win_left_x + 19 + (n - 1) * 38, self.subcat_y

    def slot_xy(self, idx: int) -> tuple[int, int]:
        n = max(1, min(14, int(idx)))
        return self.win_left_x + 268 + (n - 1) * 40, self.nav_bottom_y + 22

    def canvas_xy(self, norm_x: float, norm_y: float) -> tuple[int, int]:
        nx = max(60.0, min(940.0, float(norm_x))) / 1000.0
        ny = max(60.0, min(940.0, float(norm_y))) / 1000.0
        px = round(self.canvas_x1 + nx * max(100, self.canvas_x2 - self.canvas_x1))
        py = round(self.canvas_y1 + ny * max(100, self.canvas_y2 - self.canvas_y1))
        return px, py


def detect_pt_calibration_from_array(
    arr: np.ndarray,
    pt_window: dict[str, Any],
    monitor_name: str,
    mon_origin_x: int,
    mon_origin_y: int,
    work_bottom: int = 0,
) -> PTCalibration:
    """Detect Packet Tracer's dark-navy divider strip and derive exact global toolbar + canvas pixels."""
    wx_global, wy_global = pt_window["at"]
    ww, wh = pt_window["size"]

    wx = max(0, int(wx_global - mon_origin_x))
    wy = max(0, int(wy_global - mon_origin_y))
    x2 = min(arr.shape[1], wx + int(ww))
    y_start = max(0, wy + int(wh * 0.50))
    y_end = min(arr.shape[0], wy + int(wh) + 30)
    if x2 <= wx + 200 or y_end <= y_start + 60:
        raise RuntimeError(f"Jendela Packet Tracer terlalu kecil atau terpotong ({ww}x{wh}).")

    sub = arr[y_start:y_end, wx:x2].astype(int)
    navy = (sub[:, :, 2] - sub[:, :, 0] > 18) & (sub[:, :, 0] < 65) & (sub[:, :, 2] < 130)
    row_cov = navy.mean(axis=1)
    bar_rows = np.where(row_cov > 0.40)[0]
    if len(bar_rows) < 6:
        raise RuntimeError("Bar navigasi Packet Tracer tidak terdeteksi—pastikan jendela Packet Tracer terlihat penuh.")

    splits = np.where(np.diff(bar_rows) > 3)[0]
    first_block = bar_rows[: splits[0] + 1] if len(splits) else bar_rows
    nav_bottom_local = y_start + int(first_block[-1]) + 1
    mid_row = int(first_block[len(first_block) // 2])
    left_cols = np.where(navy[mid_row])[0]
    win_left_local = wx + (int(left_cols[0]) if len(left_cols) else 0)

    win_left_x = mon_origin_x + win_left_local
    nav_bottom_y = mon_origin_y + nav_bottom_local
    win_top_y = mon_origin_y + wy

    canvas_x1 = win_left_x + 36
    canvas_y1 = win_top_y + 115
    canvas_x2 = min(mon_origin_x + x2 - 36, win_left_x + int(ww) - 36)
    canvas_y2 = max(canvas_y1 + 140, nav_bottom_y - 26)

    return PTCalibration(
        monitor=monitor_name,
        window_address=str(pt_window.get("address") or ""),
        win_left_x=win_left_x,
        win_top_y=win_top_y,
        win_w=int(ww),
        win_h=int(wh),
        nav_bottom_y=nav_bottom_y,
        cat_y=nav_bottom_y + 27,
        subcat_y=nav_bottom_y + 100,
        search_x=win_left_x + 103,
        search_y=nav_bottom_y + 64,
        canvas_x1=canvas_x1,
        canvas_y1=canvas_y1,
        canvas_x2=canvas_x2,
        canvas_y2=canvas_y2,
        work_bottom=int(work_bottom or 0),
    )


def _split_pt_windows(windows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Identify the main Cisco Packet Tracer workspace window and all child/modal dialogs."""
    pt_wins = [w for w in windows if "packettracer" in str(w.get("class") or "").lower()]
    if not pt_wins:
        return None, []

    def _main_score(w: dict[str, Any]) -> tuple[int, int, int]:
        title = str(w.get("title") or "").strip().lower()
        ww, wh = w.get("size") or (0, 0)
        area = int(ww) * int(wh)
        is_main_title = 1 if (title == "cisco packet tracer" or title.startswith("cisco packet tracer -")) else 0
        return (
            is_main_title,
            0 if w.get("floating") else 1,
            area,
        )

    main_pt = max(pt_wins, key=_main_score)
    children = [w for w in pt_wins if w.get("address") != main_pt.get("address")]
    return main_pt, children


def _close_pt_windows_list(wins: list[dict[str, Any]]) -> None:
    """Close Packet Tracer child windows cleanly via Win32 WM_CLOSE."""
    for w in wins:
        addr = str(w.get("address") or "")
        if not addr:
            continue
        wb.close_window(addr)
        time.sleep(0.16)


def _is_pt_hover_tooltip(w: dict[str, Any]) -> bool:
    """Return True if a floating PacketTracer surface is a small Qt hover tooltip."""
    if not w.get("floating"):
        return False
    title = str(w.get("title") or "").strip().lower()
    win_cls = str(w.get("win_class") or "").lower()
    _, wh = w.get("size") or (0, 0)
    if "tooltip" in win_cls:
        return True
    return title in ("cisco packet tracer", "") and int(wh) <= 44


def _is_pt_port_menu(w: dict[str, Any]) -> bool:
    """Return True if a floating PacketTracer surface looks like a cable port QMenu popup."""
    if not w.get("floating"):
        return False
    title = str(w.get("title") or "").strip().lower()
    ww, wh = w.get("size") or (0, 0)
    return title in ("cisco packet tracer", "") and 60 <= int(ww) <= 380 and int(wh) >= 18


def focus_and_calibrate_pt(desktop: DesktopController) -> PTCalibration:
    """Focus the Cisco Packet Tracer window on Windows and return dynamic pixel calibration."""
    main_pt, children = _split_pt_windows(desktop.get_windows())
    if main_pt is None:
        raise RuntimeError("Cisco Packet Tracer tidak sedang terbuka.")

    # Auto-close stale non-floating child device dialogs (left open from an interrupted run)
    stale_device_dlgs = [w for w in children if not w.get("floating")]
    if stale_device_dlgs:
        _close_pt_windows_list(stale_device_dlgs)
        main_pt, children = _split_pt_windows(desktop.get_windows())
        if main_pt is None:
            raise RuntimeError("Cisco Packet Tracer tidak sedang terbuka.")

    mons = desktop.get_monitors()
    mx = main_pt["at"][0] + main_pt["size"][0] // 2
    my = main_pt["at"][1] + main_pt["size"][1] // 2
    target_mon = next(
        (m for m in mons if int(m["x"]) <= mx < int(m["x"]) + int(m["logical_w"]) and int(m["y"]) <= my < int(m["y"]) + int(m["logical_h"])),
        mons[0],
    )

    wb.focus_window(f'address:{main_pt["address"]}')
    time.sleep(0.22)

    # Dismiss stray QMenu popups (leaving tooltips ignored so hovering toolbar buttons never aborts)
    if any(_is_pt_port_menu(w) and not _is_pt_hover_tooltip(w) for w in children):
        try:
            desktop.press_hotkey("Escape")
            time.sleep(0.10)
        except Exception:
            pass
        _, children = _split_pt_windows(desktop.get_windows())

    modal = next((w for w in children if w.get("floating") and not _is_pt_hover_tooltip(w)), None)
    if modal is not None:
        raise RuntimeError(
            f"Ada dialog popup '{modal.get('title') or 'Dialog'}' terbuka di Packet Tracer. Tutup/selesaikan dialog dulu sebelum bangun topologi."
        )

    main_pt = next(
        (w for w in desktop.get_windows() if w.get("address") == main_pt["address"]),
        main_pt,
    )
    rgb, meta = desktop._capture_monitor_image(str(target_mon["name"]))
    arr = np.array(rgb)
    return detect_pt_calibration_from_array(
        arr,
        main_pt,
        meta.monitor,
        meta.origin_x,
        meta.origin_y,
        work_bottom=int(target_mon.get("work_bottom") or 0),
    )


def _clear_pt_canvas(desktop: DesktopController, cal: PTCalibration) -> PTCalibration:
    """Reset Packet Tracer workspace to a blank topology via Ctrl+N, dismissing any Save prompt cleanly."""
    _close_pt_child_windows(desktop)
    wb.focus_window(f'address:{cal.window_address}')
    time.sleep(0.08)
    desktop.click(cal.canvas_x1 + 36, cal.canvas_y1 + 36, restore_cursor=False, glide_ms=30, pre_hover_ms=30)
    time.sleep(0.06)
    desktop.press_hotkey("ctrl+n")
    time.sleep(0.42)

    _, children = _split_pt_windows(desktop.get_windows())
    for w in children:
        if not w.get("floating"):
            continue
        ax, ay = w["at"]
        aw, ah = w["size"]
        wb.focus_window(f'address:{w["address"]}')
        time.sleep(0.06)
        im_d, mt_d = desktop._capture_monitor_image(cal.monitor)
        cx1 = max(0, ax - mt_d.origin_x)
        cy1 = max(0, ay - mt_d.origin_y)
        cx2 = min(im_d.width, cx1 + aw)
        cy2 = min(im_d.height, cy1 + ah)
        btns = _ocr_crop_elements(im_d.crop((cx1, cy1, cx2, cy2)), scale=2)
        no_btn = next((e for e in btns if re.sub(r"[^a-z]", "", e.text.lower()) in ("no", "discard")), None)
        if no_btn is not None:
            desktop.click(
                ax + round((no_btn.x / 1000.0) * aw),
                ay + round((no_btn.y / 1000.0) * ah),
                restore_cursor=False,
                glide_ms=30,
                label="Discard canvas",
                pre_hover_ms=45,
            )
        else:
            desktop.press_hotkey("Tab")
            time.sleep(0.05)
            desktop.press_hotkey("Return")
        time.sleep(0.28)
        break

    _RECENT_PT_NORM.clear()
    return focus_and_calibrate_pt(desktop)


def _pick_cable_probe_point(
    pt1: tuple[int, int],
    pt2: tuple[int, int],
    cal: PTCalibration,
    avoid_points: list[tuple[int, int]] | None = None,
    offset_px: int = 85,
) -> tuple[int, int]:
    """Pick an open canvas spot ~85px perpendicular to pt2 so any dangling rubber-band wire diverges visibly."""
    dx = pt2[0] - pt1[0]
    dy = pt2[1] - pt1[1]
    dist = math.hypot(dx, dy)
    if dist < 1.0:
        px, py = -1.0, 0.0
    else:
        px, py = -dy / dist, dx / dist

    min_x = cal.canvas_x1 + 28
    max_x = max(min_x + 1, cal.canvas_x2 - 28)
    min_y = cal.canvas_y1 + 28
    max_y = max(min_y + 1, cal.canvas_y2 - 28)

    avoid = list(avoid_points or [])
    best: tuple[float, int, int] | None = None
    for sign in (1.0, -1.0):
        cx = int(round(max(min_x, min(max_x, pt2[0] + sign * px * offset_px))))
        cy = int(round(max(min_y, min(max_y, pt2[1] + sign * py * offset_px))))
        if math.hypot(cx - pt2[0], cy - pt2[1]) < 40:
            continue
        clearance = min((math.hypot(cx - ax, cy - ay) for ax, ay in avoid), default=999.0)
        if best is None or clearance > best[0]:
            best = (clearance, cx, cy)
    if best is not None:
        return best[1], best[2]
    cx = pt2[0] + (-offset_px if pt2[0] > (min_x + max_x) // 2 else offset_px)
    cy = pt2[1] + (-40 if pt2[1] > (min_y + max_y) // 2 else 40)
    return int(max(min_x, min(max_x, cx))), int(max(min_y, min(max_y, cy)))


def _segment_change_score(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
    mon_ox: int,
    mon_oy: int,
    pt_a: tuple[int, int],
    pt_b: tuple[int, int],
    samples: int = 28,
) -> int:
    """Count sampled corridor points along pt_a -> pt_b (including bowed parallel EtherChannel links) that changed."""
    h, w = after_arr.shape[:2]
    dx = pt_b[0] - pt_a[0]
    dy = pt_b[1] - pt_a[1]
    dist = math.hypot(dx, dy)
    if dist < 45.0:
        return 0

    ux, uy = dx / dist, dy / dist
    px, py = -uy, ux

    margin_t = min(0.40, 32.0 / dist)
    t_start = max(0.25, margin_t)
    t_end = min(0.78, 1.0 - margin_t)
    if t_end <= t_start:
        t_start, t_end = 0.42, 0.58

    diff = np.abs(after_arr.astype(np.int16) - before_arr.astype(np.int16)).max(axis=-1)
    hits = 0
    for i in range(samples):
        t = t_start + (t_end - t_start) * (i / max(1, samples - 1))
        bx = pt_a[0] + dx * t - mon_ox
        by = pt_a[1] + dy * t - mon_oy
        hit_sample = False
        for perp in (0, -6, 6, -12, 12, -16, 16):
            gx = int(round(bx + px * perp))
            gy = int(round(by + py * perp))
            x1, x2 = max(0, gx - 2), min(w, gx + 3)
            y1, y2 = max(0, gy - 2), min(h, gy + 3)
            if x2 > x1 and y2 > y1 and bool((diff[y1:y2, x1:x2] > 35).any()):
                hit_sample = True
                break
        if hit_sample:
            hits += 1
    return hits


def _detect_cable_state(
    before_arr: np.ndarray,
    after_arr: np.ndarray,
    mon_ox: int,
    mon_oy: int,
    pt1: tuple[int, int],
    pt2: tuple[int, int],
    probe: tuple[int, int],
) -> str:
    """Classify a cable attempt as 'connected', 'dangling', or 'missing'."""
    target_hits = _segment_change_score(before_arr, after_arr, mon_ox, mon_oy, pt1, pt2)
    probe_hits = _segment_change_score(before_arr, after_arr, mon_ox, mon_oy, pt1, probe)
    if probe_hits >= 5 and probe_hits > target_hits:
        return "dangling"
    if target_hits >= 3:
        return "connected"
    return "missing"


def _normalize_port_query(raw: str) -> tuple[str, str]:
    """Parse a port string (e.g. 'G0/0/1', 'Fa0/21', 'FastEthernet0') into (family, slash_digits)."""
    s = re.sub(r"\s+", "", str(raw or "").strip().lower())
    s = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", s)
    if not s:
        return ("", "")
    s = re.sub(r"(?<=[a-z/])o(?=[/\d])|(?<=/)o(?=$|[^a-z])", "0", s)
    m_embed = re.search(
        r"(gigabitethernet|fastethernet|ethernet|serial|console|aux|vlan|(?:gigabit|fast|ser|eth|gi|fa|se|vl|[gfse])(?=\d))(\d+(?:/\d+)*)?",
        s,
    )
    if m_embed and any(k in s for k in ("[", "]", "|", "'")):
        s = (m_embed.group(1) or "") + (m_embed.group(2) or "")
    if "cons" in s:
        return ("console", "")
    if "aux" in s:
        return ("aux", "")
    m_num = re.search(r"^([a-z-]+?)(\d+(?:/\d+)*)$", s)
    if m_num:
        prefix, nums = m_num.group(1), m_num.group(2)
    else:
        return ("", "")
    if "gig" in prefix or prefix in ("g", "gi", "gigabit", "gigabiteth"):
        fam = "gig"
    elif "fast" in prefix or prefix in ("f", "fa", "fasteth"):
        fam = "fast"
    elif "ser" in prefix or prefix in ("s", "se"):
        fam = "serial"
    elif "eth" in prefix or prefix in ("e", "et"):
        fam = "eth"
    elif "vlan" in prefix or prefix == "vl":
        fam = "vlan"
    else:
        return ("", "")
    return (fam, nums)


def _match_port_menu_item(els: list[Any], want_port: str) -> Any | None:
    """Match an OCR'd Packet Tracer cable port context-menu row exactly."""
    want_fam, want_nums = _normalize_port_query(want_port)
    for e in els:
        efam, enums = _normalize_port_query(getattr(e, "text", ""))
        if not efam:
            continue
        if not want_port.strip():
            if efam in ("gig", "fast", "eth", "serial"):
                return e
            continue
        if efam == want_fam and enums == want_nums:
            return e
    return None


def _infer_cable_for_pair(model_a: str, name_a: str, model_b: str, name_b: str) -> str:
    """Infer straight vs cross-over cable when ports are explicitly requested on 'auto'."""
    fa = _device_family(model_a) or _device_family(name_a)
    fb = _device_family(model_b) or _device_family(name_b)
    if not fa or not fb:
        return "straight"
    same_side = {
        ("switch", "switch"),
        ("router", "router"),
        ("pc", "pc"),
        ("laptop", "laptop"),
        ("server", "server"),
        ("pc", "laptop"),
        ("pc", "server"),
        ("laptop", "server"),
        ("router", "pc"),
        ("router", "laptop"),
        ("router", "server"),
    }
    if (fa, fb) in same_side or (fb, fa) in same_side:
        return "cross"
    return "straight"


def _select_pt_port(
    desktop: DesktopController,
    cal: PTCalibration,
    dev_pt: tuple[int, int],
    want_port: str,
    glide_ms: int,
    label_prefix: str = "Port",
) -> str:
    """Capture Packet Tracer's floating QMenu port popup, find want_port accurately, and click/confirm it."""
    menu_w: dict[str, Any] | None = None
    wrong_dlg: dict[str, Any] | None = None
    for _ in range(7):
        time.sleep(0.07)
        _, children = _split_pt_windows(desktop.get_windows())
        for w in children:
            at = w.get("at") or (0, 0)
            sz = w.get("size") or (0, 0)
            ww, wh = int(sz[0]), int(sz[1])
            if _is_pt_port_menu(w) and (int(wh) > 44 or (abs(int(at[0]) - dev_pt[0]) <= 280 and abs(int(at[1]) - dev_pt[1]) <= 180)):
                menu_w = w
                break
            if not _is_pt_hover_tooltip(w) and (not w.get("floating") or ww > 380 or str(w.get("title") or "").strip().lower() not in ("cisco packet tracer", "")):
                wrong_dlg = w
        if menu_w is not None or wrong_dlg is not None:
            break

    if wrong_dlg is not None and menu_w is None:
        _close_pt_child_windows(desktop)
        raise RuntimeError(f"Klik '{label_prefix}' membuka jendela '{wrong_dlg.get('title')}' bukan menu port.")

    win_bottom = cal.win_top_y + cal.win_h
    safe_bottom = min(win_bottom - 3, (cal.work_bottom - 3) if cal.work_bottom else (win_bottom - 3))

    if menu_w is not None:
        addr = str(menu_w.get("address") or "")
        mx, my = int(menu_w["at"][0]), int(menu_w["at"][1])
        mw, mh = int(menu_w["size"][0]), int(menu_w["size"][1])
        img_m, meta_m = _capture_clean_monitor(desktop, cal.monitor)
        ox, oy = meta_m.origin_x, meta_m.origin_y
        cx1 = max(0, mx - ox)
        cy1 = max(0, my - oy)
        cx2 = min(img_m.width, cx1 + mw)
        cy2 = min(img_m.height, cy1 + mh)
        crop = img_m.crop((cx1, cy1, cx2, cy2))
        border = 16
        padded = ImageOps.expand(crop, border=border, fill=(255, 255, 255))

        def _wait_menu_closed() -> bool:
            if not addr:
                time.sleep(0.08)
                return True
            for _ in range(8):
                _, cur_children = _split_pt_windows(desktop.get_windows())
                if all(w.get("address") != addr for w in cur_children):
                    return True
                time.sleep(0.04)
            return False

        def _keyboard_pick_menu_item(target_el: Any, all_els: list[Any]) -> bool:
            rows = sorted(
                [e for e in all_els if _normalize_port_query(getattr(e, "text", ""))[0]],
                key=lambda e: (e.y, e.x),
            )
            idx = next((i for i, e in enumerate(rows) if e is target_el), -1)
            if idx < 0:
                return False
            rev_idx = len(rows) - 1 - idx
            if rev_idx <= 4 and target_el.y >= 720:
                for _ in range(rev_idx + 1):
                    desktop.press_hotkey("Up")
                    time.sleep(0.045)
            else:
                for _ in range(idx + 1):
                    desktop.press_hotkey("Down")
                    time.sleep(0.045)
            desktop.press_hotkey("Return")
            return _wait_menu_closed()

        if mh <= 34:
            lines = _ocr_cli_lines(crop) or _ocr_cli_lines(ImageOps.invert(crop.convert("L"))) or _ocr_cli_lines(padded)
            cand_txt = lines[0] if lines else ""
            if (
                not want_port.strip()
                or not cand_txt
                or _normalize_port_query(cand_txt) == _normalize_port_query(want_port)
            ):
                px, py = mx + mw // 2, my + mh // 2
                closed = False
                if py <= safe_bottom:
                    desktop.click(px, py, restore_cursor=False, glide_ms=glide_ms, label=f"{label_prefix} {cand_txt or want_port}", pre_hover_ms=45)
                    closed = _wait_menu_closed()
                if not closed:
                    desktop.press_hotkey("Down")
                    time.sleep(0.045)
                    desktop.press_hotkey("Return")
                    closed = _wait_menu_closed()
                if not closed:
                    desktop.press_hotkey("Escape")
                    raise RuntimeError(f"Gagal memilih port '{cand_txt or want_port}' di menu PT.")
                return cand_txt or want_port
            desktop.press_hotkey("Escape")
            raise RuntimeError(f"Port '{want_port}' tidak ditemukan di menu PT (terbaca: {[cand_txt]})")

        max_tries = 2 if mh >= 520 else 1
        menu_els: list[Any] = []
        for attempt in range(max_tries):
            if attempt == 1:
                desktop.scroll(mx + mw // 2, my + mh // 2, dy=-6)
                time.sleep(0.18)
                img_m, meta_m = _capture_clean_monitor(desktop, cal.monitor)
                ox, oy = meta_m.origin_x, meta_m.origin_y
                crop = img_m.crop((max(0, mx - ox), max(0, my - oy), min(img_m.width, mx - ox + mw), min(img_m.height, my - oy + mh)))
                padded = ImageOps.expand(crop, border=border, fill=(255, 255, 255))

            menu_els = _ocr_crop_elements(padded, scale=3)
            hit = _match_port_menu_item(menu_els, want_port)
            if hit is not None:
                px = mx + max(4, min(mw - 4, round((hit.x / 1000.0) * (mw + border * 2) - border)))
                py = my + max(4, min(mh - 4, round((hit.y / 1000.0) * (mh + border * 2) - border)))
                box = getattr(hit, "box", (0, max(0, hit.y - 8), 1000, min(1000, hit.y + 8)))
                item_top = my + max(4, min(mh - 4, round((box[1] / 1000.0) * (mh + border * 2) - border)))

                click_ys: list[int] = []
                if py <= safe_bottom:
                    click_ys.append(py)
                    upper_y = max(my + 5, item_top - 1)
                    if upper_y < py and upper_y <= safe_bottom:
                        click_ys.append(upper_y)
                elif item_top - 4 <= safe_bottom:
                    clamped_y = max(my + 5, min(safe_bottom, item_top + 1))
                    click_ys.append(clamped_y)
                    upper_y = max(my + 5, clamped_y - 4)
                    if upper_y < clamped_y:
                        click_ys.append(upper_y)

                closed = False
                for cy in click_ys:
                    desktop.click(px, cy, restore_cursor=False, glide_ms=glide_ms, label=f"{label_prefix} {hit.text}", pre_hover_ms=45)
                    closed = _wait_menu_closed()
                    if closed:
                        break
                if not closed:
                    closed = _keyboard_pick_menu_item(hit, menu_els)
                if not closed:
                    desktop.press_hotkey("Escape")
                    time.sleep(0.06)
                    raise RuntimeError(f"Gagal memilih port '{hit.text}' di menu PT (menu tidak tertutup).")
                return str(hit.text)

        avail = [e.text for e in menu_els if _normalize_port_query(e.text)[0] in ("gig", "fast", "serial", "eth", "console")]
        desktop.press_hotkey("Escape")
        raise RuntimeError(f"Port '{want_port}' tidak ditemukan di menu PT (terbaca: {avail[:16]})")

    time.sleep(0.14)
    img_m, meta_m = _capture_clean_monitor(desktop, cal.monitor)
    ox, oy = meta_m.origin_x, meta_m.origin_y
    mx1 = max(0, dev_pt[0] - 260 - ox)
    mx2 = min(img_m.width, dev_pt[0] + 380 - ox)
    crop = img_m.crop((mx1, 0, mx2, img_m.height))
    menu_els = _ocr_crop_elements(crop, scale=2)
    hit = _match_port_menu_item(menu_els, want_port)
    if hit is None:
        avail = [e.text for e in menu_els if _normalize_port_query(e.text)[0] in ("gig", "fast", "serial", "eth", "console")][:10]
        raise RuntimeError(f"Port '{want_port}' tidak ditemukan di menu PT (terbaca: {avail})")
    px = mx1 + ox + round((hit.x / 1000.0) * (mx2 - mx1))
    py = min(safe_bottom, oy + round((hit.y / 1000.0) * img_m.height))
    desktop.click(
        px,
        py,
        restore_cursor=False,
        glide_ms=glide_ms,
        label=f"{label_prefix} {hit.text}",
        pre_hover_ms=45,
    )
    return str(hit.text)


def disarm_pt_tools(desktop: DesktopController, cal: PTCalibration | None = None) -> None:
    """Cancel any live cable/placement tool (and open QMenu popups) in Packet Tracer."""
    pt_main = None
    try:
        pt_main, children = _split_pt_windows(desktop.get_windows())
        if any(_is_pt_port_menu(w) and not _is_pt_hover_tooltip(w) for w in children):
            desktop.press_hotkey("Escape")
            time.sleep(0.06)
    except Exception:
        pass
    if desktop.disarm_if_packet_tracer():
        return
    if pt_main is not None and (cal is None or pt_main.get("address") == cal.window_address):
        wb.focus_window(f'address:{pt_main["address"]}')
        time.sleep(0.08)
        try:
            desktop.press_hotkey("Escape")
            time.sleep(0.05)
            desktop.press_hotkey("Escape")
        except Exception:
            pass


def build_packet_tracer_topology(
    desktop: DesktopController,
    devices: list[dict[str, Any]],
    connections: list[dict[str, Any]] | None,
    stop_event: threading.Event,
    pause_gate: Any,
    glide_ms: int = 75,
    restore_cursor: bool = True,
    clear_canvas: bool = False,
    fast_forward: bool = True,
) -> dict[str, Any]:
    """Calibrate Packet Tracer, optionally reset canvas, place devices + cables cleanly."""
    cal = focus_and_calibrate_pt(desktop)
    if clear_canvas and not stop_event.is_set():
        cal = _clear_pt_canvas(desktop, cal)
    step_glide = min(glide_ms, 80)

    placed: dict[str, tuple[int, int]] = {}
    placed_models: dict[str, str] = {}
    placed_points: list[tuple[int, int]] = []
    placed_summary: list[dict[str, Any]] = []
    search_dirty = False
    last_cat_key: tuple[str, int] | None = None
    last_search_query: str = ""
    cables_made: list[dict[str, Any]] = []
    canvas_scan_cache: tuple[list[Any], list[tuple[int, int, list[str]]]] | None = None

    def _clear_search_if_dirty() -> None:
        nonlocal search_dirty, last_search_query
        if not search_dirty:
            return
        desktop.click(cal.search_x, cal.search_y, restore_cursor=False, glide_ms=step_glide, label="Reset search")
        time.sleep(0.06)
        desktop.press_hotkey("ctrl+a")
        desktop.press_hotkey("BackSpace")
        time.sleep(0.10)
        search_dirty = False
        last_search_query = ""

    def _capture_canvas() -> tuple[np.ndarray, int, int] | None:
        try:
            img, meta = desktop._capture_monitor_image(cal.monitor)
            return np.array(img), meta.origin_x, meta.origin_y
        except Exception:
            return None

    def _resolve_existing_device_point(ref: str) -> tuple[int, int] | None:
        nonlocal canvas_scan_cache
        r_low = ref.lower()
        cached_norm = _RECENT_PT_NORM.get(r_low)
        if cached_norm is not None:
            return cal.canvas_xy(cached_norm[0], cached_norm[1])
        if canvas_scan_cache is None:
            canvas_scan_cache = _scan_canvas_devices(desktop, cal)
        ocr_items, blobs = canvas_scan_cache
        hit = _match_canvas_label([it[0] for it in ocr_items], ref)
        if hit is not None:
            matched_item = next((it for it in ocr_items if it[0] is hit), None)
            if matched_item is not None:
                return (matched_item[1], max(cal.canvas_y1 + 36, matched_item[2] - 34))
        r_norm = _normalize_ocr_label(r_low)
        for icx, icy, lbls in blobs:
            if any(_normalize_ocr_label(l) == r_norm for l in lbls):
                return (icx, icy)
        return None

    _shield = desktop.physical_cursor_shield()
    _shield.__enter__()
    try:
        for idx, dev in enumerate(devices[:20], start=1):
            pause_gate()
            if stop_event.is_set():
                break
            name = str(dev.get("name") or f"Dev{idx}").strip()
            model_raw = str(dev.get("model") or "PC-PT").strip()
            model_key = model_raw.lower().strip()
            nx = float(dev.get("x", 500))
            ny = float(dev.get("y", 500))
            px, py = cal.canvas_xy(nx, ny)

            known = KNOWN_DEVICE_SLOTS.get(model_key)
            if known is not None:
                cat, subcat, slot = known
                _clear_search_if_dirty()
                if last_cat_key != (cat, subcat):
                    cx, cy = cal.category_xy(cat)
                    desktop.click(cx, cy, restore_cursor=False, glide_ms=step_glide, label=cat.title())
                    time.sleep(0.14)
                    sx, sy = cal.subcat_xy(subcat)
                    desktop.click(sx, sy, restore_cursor=False, glide_ms=step_glide, label=f"Sub {subcat}")
                    time.sleep(0.14)
                    last_cat_key = (cat, subcat)
                slx, sly = cal.slot_xy(slot)
                desktop.click(slx, sly, restore_cursor=False, glide_ms=step_glide, label=model_raw)
            else:
                last_cat_key = None
                if last_search_query.lower() != model_raw.lower():
                    desktop.click(cal.search_x, cal.search_y, restore_cursor=False, glide_ms=step_glide, label="Cari device")
                    time.sleep(0.06)
                    desktop.press_hotkey("ctrl+a")
                    desktop.press_hotkey("BackSpace")
                    desktop.type_text(model_raw, press_enter=False)
                    time.sleep(0.22)
                    search_dirty = True
                    last_search_query = model_raw
                slx, sly = cal.slot_xy(1)
                desktop.click(slx, sly, restore_cursor=False, glide_ms=step_glide, label=model_raw)

            if stop_event.is_set():
                break
            time.sleep(0.08)
            desktop.click(px, py, restore_cursor=False, glide_ms=step_glide, label=f"Taruh {name}", pre_hover_ms=50)
            time.sleep(0.12)
            placed[name] = (px, py)
            placed[name.lower()] = (px, py)
            placed_models[name.lower()] = model_raw
            _RECENT_PT_NORM[name.lower()] = (nx, ny)
            placed_points.append((px, py))
            placed_summary.append({
                "name": name,
                "model": model_raw,
                "canvas_x": round(nx),
                "canvas_y": round(ny),
                "global_x": px,
                "global_y": py,
            })

        conns = list(connections or [])
        if conns and not stop_event.is_set():
            _clear_search_if_dirty()
            cnx, cny = cal.category_xy("connections")
            desktop.click(cnx, cny, restore_cursor=False, glide_ms=step_glide, label="Connections")
            time.sleep(0.18)

            prev_slot: int | None = None
            for conn in conns[:40]:
                pause_gate()
                if stop_event.is_set():
                    break
                frm = str(conn.get("from") or "").strip()
                to = str(conn.get("to") or "").strip()
                from_port = str(conn.get("from_port") or "").strip()
                to_port = str(conn.get("to_port") or "").strip()
                cable = str(conn.get("cable") or "auto").lower().strip()

                pt1 = placed.get(frm) or placed.get(frm.lower()) or _resolve_existing_device_point(frm)
                pt2 = placed.get(to) or placed.get(to.lower()) or _resolve_existing_device_point(to)
                if pt1 is None or pt2 is None:
                    cables_made.append({"from": frm, "to": to, "ok": False, "error": "Nama device tidak ditemukan"})
                    continue

                if cable == "auto" and (from_port or to_port):
                    cable = _infer_cable_for_pair(
                        placed_models.get(frm.lower(), ""),
                        frm,
                        placed_models.get(to.lower(), ""),
                        to,
                    )
                slot = CABLE_SLOTS.get(cable, 1)

                if prev_slot is not None and slot != prev_slot:
                    disarm_pt_tools(desktop, cal)
                    time.sleep(0.08)
                prev_slot = slot

                probe = _pick_cable_probe_point(pt1, pt2, cal, avoid_points=placed_points)
                before_snap = _capture_canvas()

                cslx, csly = cal.slot_xy(slot)
                desktop.click(cslx, csly, restore_cursor=False, glide_ms=step_glide, label=f"Kabel {cable}", pre_hover_ms=55)
                if stop_event.is_set():
                    break
                time.sleep(0.10)
                desktop.click(pt1[0], pt1[1], restore_cursor=False, glide_ms=step_glide, label=f"Dari {frm}", pre_hover_ms=75)
                if stop_event.is_set():
                    cables_made.append({"from": frm, "to": to, "cable": cable, "ok": False, "error": "Dihentikan saat menarik kabel"})
                    break

                picked_from = ""
                picked_to = ""
                if slot != 1:
                    try:
                        picked_from = _select_pt_port(desktop, cal, pt1, from_port, step_glide, label_prefix=frm)
                    except Exception as port_err:
                        disarm_pt_tools(desktop, cal)
                        cables_made.append({"from": frm, "to": to, "from_port": from_port, "cable": cable, "ok": False, "error": str(port_err)})
                        continue

                time.sleep(0.24 if slot != 1 else 0.11)
                desktop.click(pt2[0], pt2[1], restore_cursor=False, glide_ms=step_glide, label=f"Ke {to}", pre_hover_ms=85)

                if slot != 1:
                    try:
                        picked_to = _select_pt_port(desktop, cal, pt2, to_port, step_glide, label_prefix=to)
                    except Exception as port_err:
                        disarm_pt_tools(desktop, cal)
                        cables_made.append({"from": frm, "to": to, "from_port": picked_from or from_port, "to_port": to_port, "cable": cable, "ok": False, "error": str(port_err)})
                        continue

                time.sleep(0.14)
                state = "connected"
                if before_snap is not None:
                    before_arr, ox, oy = before_snap
                    desktop.move_physical_pointer(probe[0], probe[1])
                    time.sleep(0.06)
                    after_snap = _capture_canvas()
                    if after_snap is not None:
                        state = _detect_cable_state(before_arr, after_snap[0], ox, oy, pt1, pt2, probe)

                        if slot == 1:
                            for off_y in (0, -8, 8):
                                if state != "dangling" or stop_event.is_set():
                                    break
                                desktop.click(
                                    pt2[0],
                                    pt2[1] + off_y,
                                    restore_cursor=False,
                                    glide_ms=step_glide,
                                    label=f"Ulang ke {to}",
                                    pre_hover_ms=105,
                                )
                                time.sleep(0.12)
                                desktop.move_physical_pointer(probe[0], probe[1])
                                time.sleep(0.06)
                                retry_snap = _capture_canvas()
                                if retry_snap is None:
                                    break
                                state = _detect_cable_state(before_arr, retry_snap[0], ox, oy, pt1, pt2, probe)

                            if state == "missing" and not stop_event.is_set():
                                disarm_pt_tools(desktop, cal)
                                desktop.click(cslx, csly, restore_cursor=False, glide_ms=step_glide, label=f"Kabel {cable}", pre_hover_ms=55)
                                time.sleep(0.10)
                                desktop.click(pt1[0], pt1[1], restore_cursor=False, glide_ms=step_glide, label=f"Dari {frm}", pre_hover_ms=85)
                                time.sleep(0.12)
                                desktop.click(pt2[0], pt2[1], restore_cursor=False, glide_ms=step_glide, label=f"Ke {to}", pre_hover_ms=95)
                                time.sleep(0.12)
                                desktop.move_physical_pointer(probe[0], probe[1])
                                time.sleep(0.06)
                                retry_snap = _capture_canvas()
                                if retry_snap is not None:
                                    state = _detect_cable_state(before_arr, retry_snap[0], ox, oy, pt1, pt2, probe)
                        elif state == "missing" and math.hypot(pt2[0] - pt1[0], pt2[1] - pt1[1]) >= 65:
                            time.sleep(0.12)
                            retry_snap = _capture_canvas()
                            if retry_snap is not None:
                                state = _detect_cable_state(before_arr, retry_snap[0], ox, oy, pt1, pt2, probe)
                                if state == "missing" and _segment_change_score(before_arr, retry_snap[0], ox, oy, pt1, pt2) > 0:
                                    state = "connected"

                if state == "dangling" or (state == "missing" and slot != 1 and math.hypot(pt2[0] - pt1[0], pt2[1] - pt1[1]) >= 65):
                    disarm_pt_tools(desktop, cal)
                    err_msg = (
                        "Kabel belum menempel ke target — mode kabel dibatalkan"
                        if state == "dangling"
                        else "Kabel tidak terlihat tersambung antara perangkat — mode kabel dibatalkan"
                    )
                    cables_made.append({"from": frm, "to": to, "from_port": picked_from or from_port, "to_port": picked_to or to_port, "cable": cable, "ok": False, "error": err_msg})
                else:
                    entry: dict[str, Any] = {"from": frm, "to": to, "cable": cable, "ok": True}
                    if picked_from or from_port:
                        entry["from_port"] = picked_from or from_port
                    if picked_to or to_port:
                        entry["to_port"] = picked_to or to_port
                    cables_made.append(entry)

        if fast_forward and (placed_summary or cables_made) and not stop_event.is_set():
            disarm_pt_tools(desktop, cal)
            wb.focus_window(f'address:{cal.window_address}')
            time.sleep(0.08)
            desktop.press_hotkey("alt+d")
            time.sleep(0.28)

        try:
            img_end, meta_end = _capture_clean_monitor(desktop, cal.monitor)
            end_blobs = _detect_pt_device_blobs(np.array(img_end), meta_end, cal)
            for item in placed_summary:
                gx, gy = item["global_x"], item["global_y"]
                nearest = min(end_blobs, key=lambda b: math.hypot(b[0] - gx, b[1] - gy), default=None)
                if nearest and math.hypot(nearest[0] - gx, nearest[1] - gy) < 60:
                    lbl_cand = next((l for l in reversed(nearest[2]) if l.strip()), "")
                    if lbl_cand:
                        item["pt_label"] = lbl_cand
                        _RECENT_PT_NORM[lbl_cand.lower()] = (float(item["canvas_x"]), float(item["canvas_y"]))
        except Exception:
            pass
    finally:
        try:
            disarm_pt_tools(desktop, cal)
        except Exception:
            pass
        if restore_cursor and desktop._task_orig_cursor is not None:
            try:
                desktop._restore_physical(*desktop._task_orig_cursor)
            except Exception:
                pass
        _shield.__exit__(None, None, None)

    return {
        "ok": all(c.get("ok", True) for c in cables_made),
        "monitor": cal.monitor,
        "calibration": {"win_left_x": cal.win_left_x, "nav_bottom_y": cal.nav_bottom_y},
        "devices_placed": placed_summary,
        "connections": cables_made,
    }


def _close_pt_child_windows(desktop: DesktopController) -> None:
    """Close any open Packet Tracer device/dialog child windows cleanly."""
    for _ in range(2):
        _, children = _split_pt_windows(desktop.get_windows())
        if not children:
            break
        _close_pt_windows_list(children)


def _pt_child_windows(desktop: DesktopController) -> list[dict[str, Any]]:
    _, children = _split_pt_windows(desktop.get_windows())
    return children


def _normalize_ios_script(
    commands: list[str] | str,
    current_prompt: str = "",
    verify_cli: bool = True,
) -> list[str]:
    """Normalize IOS CLI commands cleanly for reliable pasting in Packet Tracer."""
    if isinstance(commands, str):
        raw_lines = commands.replace("\r\n", "\n").splitlines()
    else:
        raw_lines = []
        for c in commands:
            raw_lines.extend(str(c).replace("\r\n", "\n").splitlines())

    lines = [ln.rstrip() for ln in raw_lines if ln.strip()]
    if not lines:
        return []

    first_low = lines[0].strip().lower()
    in_config = "(config" in current_prompt.lower() or "(vlan" in current_prompt.lower()
    in_user_exec = current_prompt.strip().endswith(">")

    out: list[str] = []
    config_starters = (
        "hostname ",
        "interface ",
        "int ",
        "ip ",
        "no ",
        "router ",
        "vlan ",
        "banner ",
        "line ",
        "service ",
        "access-list ",
        "spanning-tree ",
        "channel-group ",
        "port-channel",
        "vtp ",
        "mls ",
        "snmp-server ",
        "enable ",
    )
    needs_conf_t = any(first_low.startswith(p) for p in config_starters) and not first_low.startswith(("enable", "conf"))

    if in_config and (first_low.startswith("enable") or first_low.startswith("conf") or first_low.startswith("show ")):
        out.append("end")
    elif in_user_exec and not first_low.startswith("enable"):
        if needs_conf_t or first_low.startswith(("conf", "vlan", "show ", "write", "copy")):
            out.append("enable")

    if needs_conf_t and not in_config:
        out.append("configure terminal")

    inserted_no_lookup = False
    for ln in lines:
        low = ln.strip().lower()
        out.append(ln)
        if not inserted_no_lookup and re.match(r"^conf(?:ig(?:ure)?)?(?:\s+t(?:erm(?:inal)?)?)?$", low):
            if not any("domain-lookup" in x.lower() for x in lines):
                out.append("no ip domain-lookup")
            inserted_no_lookup = True

    if needs_conf_t and not in_config and not any("domain-lookup" in x.lower() for x in out):
        idx_ct = next((i for i, x in enumerate(out) if x.strip().lower() == "configure terminal"), -1)
        if idx_ct >= 0:
            out.insert(idx_ct + 1, "no ip domain-lookup")

    if verify_cli:
        joined_low = "\n".join(out).lower()
        did_cfg = any(
            k in joined_low
            for k in (
                "configure terminal",
                "conf t",
                "interface ",
                "int ",
                "vlan ",
                "ip address",
                "router ",
                "spanning-tree",
                "channel-group",
                "hostname ",
                "snmp-server",
            )
        )
        if did_cfg:
            if out[-1].strip().lower() != "end":
                out.append("end")
            if "show ip int" not in joined_low:
                out.append("show ip interface brief")
            if ("vlan " in joined_low or "switchport" in joined_low) and "show vlan" not in joined_low:
                out.append("show vlan brief")
            if ("trunk" in joined_low or "encapsulation dot1q" in joined_low) and "show interfaces trunk" not in joined_low and "show int trunk" not in joined_low:
                out.append("show interfaces trunk")
            if "spanning-tree" in joined_low and "show spanning-tree" not in joined_low:
                out.append("show spanning-tree")
            if ("channel-group" in joined_low or "port-channel" in joined_low) and "show etherchannel" not in joined_low:
                out.append("show etherchannel summary")
    return out


def _ocr_cli_lines(img: Image.Image) -> list[str]:
    tess = wb.find_tesseract()
    if not tess:
        return []
    gray = img.convert("L")
    if float(np.array(gray).mean()) < 110.0:
        gray = ImageOps.invert(gray)
    up = gray.resize((max(1, gray.width * 2), max(1, gray.height * 2)), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    up.save(buf, format="PNG")
    try:
        proc = subprocess.run(
            [tess, "stdin", "stdout", "--psm", "6"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=5.0,
            creationflags=_CREATE_NO_WINDOW,
        )
        txt = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return []
    return [ln.strip() for ln in txt.splitlines() if ln.strip()]


def _capture_clean_monitor(desktop: DesktopController, monitor: str):
    desktop.emit({"type": "capture_hide", "hidden": True})
    time.sleep(0.03)
    try:
        return desktop._capture_monitor_image(monitor)
    finally:
        desktop.emit({"type": "capture_hide", "hidden": False})


def _ocr_crop_elements(img: Image.Image, scale: int = 2):
    up = img.resize((max(1, img.width * scale), max(1, img.height * scale)), Image.Resampling.LANCZOS)
    return _collect_ocr_elements(up, max_ocr_width=0)


def _normalize_ocr_label(text: str) -> str:
    s = re.sub(r"([a-zA-Z])[oqOQ](?=\b|\d|$)", r"\g<1>0", text.strip())
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _match_canvas_label(els: list[Any], target: str):
    q = target.strip().lower()
    if not q:
        return None
    exact = [e for e in els if e.text.strip().lower() == q]
    if exact:
        return exact[0]
    pattern = re.compile(rf"(?:^|\b){re.escape(q)}(?:\b|$)", re.IGNORECASE)
    word_hits = [e for e in els if pattern.search(e.text.strip())]
    if word_hits:
        return word_hits[0]
    q_norm = _normalize_ocr_label(q)
    for e in els:
        en = _normalize_ocr_label(e.text)
        if en == q_norm or (len(q_norm) >= 2 and en.startswith(q_norm) and not en[len(q_norm) : len(q_norm) + 1].isdigit()):
            return e
    hits = match_elements(els, target)
    return hits[0] if hits else None


def _detect_pt_device_blobs(arr: np.ndarray, meta: Any, cal: PTCalibration) -> list[tuple[int, int, list[str]]]:
    """Detect Cisco Packet Tracer teal/cyan device icons cleanly on the white canvas and OCR each icon's label."""
    cx1 = max(0, cal.canvas_x1 - meta.origin_x)
    cy1 = max(0, cal.canvas_y1 - meta.origin_y + 30)
    cx2 = min(arr.shape[1], cal.canvas_x2 - meta.origin_x)
    cy2 = min(arr.shape[0], cal.canvas_y2 - meta.origin_y)
    if cx2 <= cx1 + 80 or cy2 <= cy1 + 80:
        return []

    sub = arr[cy1:cy2, cx1:cx2].astype(int)
    teal = (sub[:, :, 2] - sub[:, :, 0] > 18) & (sub[:, :, 1] - sub[:, :, 0] > 6) & (sub[:, :, 2] > 85) & (sub[:, :, 2] < 235)
    gs = 16
    H, W = teal.shape
    bh, bw = H // gs, W // gs
    if bh == 0 or bw == 0:
        return []
    grid = teal[: bh * gs, : bw * gs].reshape(bh, gs, bw, gs).sum(axis=(1, 3))
    ys, xs = np.where(grid >= 16)

    clusters: list[list[tuple[int, int]]] = []
    for y, x in zip(ys, xs):
        gx, gy = int(x * gs + gs // 2), int(y * gs + gs // 2)
        for c in clusters:
            mx = sum(p[0] for p in c) / len(c)
            my = sum(p[1] for p in c) / len(c)
            if abs(gx - mx) < 54 and abs(gy - my) < 54:
                c.append((gx, gy))
                break
        else:
            clusters.append([(gx, gy)])

    full_img = Image.fromarray(arr)
    devices: list[tuple[int, int, list[str]]] = []
    for c in clusters:
        mx = int(round(sum(p[0] for p in c) / len(c)))
        my = int(round(sum(p[1] for p in c) / len(c)))
        x0, x1 = max(0, mx - 38), min(W, mx + 38)
        y0, y1 = max(0, my - 38), min(H, my + 38)
        py_idx, px_idx = np.where(teal[y0:y1, x0:x1])
        if len(px_idx) < 55:
            continue
        icx = cal.canvas_x1 + x0 + int(round(px_idx.mean()))
        icy = (cal.canvas_y1 + 30) + y0 + int(round(py_idx.mean()))

        lx1 = max(0, icx - meta.origin_x - 58)
        ly1 = max(0, icy - meta.origin_y + 14)
        lx2 = min(full_img.width, icx - meta.origin_x + 58)
        ly2 = min(full_img.height, icy - meta.origin_y + 68)
        lbl_lines = _ocr_cli_lines(full_img.crop((lx1, ly1, lx2, ly2))) if lx2 > lx1 and ly2 > ly1 else []
        devices.append((icx, icy, lbl_lines))

    return sorted(devices, key=lambda d: (d[1], d[0]))


def _scan_canvas_devices(
    desktop: DesktopController,
    cal: PTCalibration,
) -> tuple[list[Any], list[tuple[int, int, list[str]]]]:
    """Capture canvas once cleanly and extract both full-canvas OCR labels and teal device icon clusters."""
    img, meta = _capture_clean_monitor(desktop, cal.monitor)
    cx1 = max(0, cal.canvas_x1 - meta.origin_x)
    cy1 = max(0, cal.canvas_y1 - meta.origin_y + 30)
    cx2 = min(img.width, cal.canvas_x2 - meta.origin_x)
    cy2 = min(img.height, cal.canvas_y2 - meta.origin_y)
    crop = img.crop((cx1, cy1, cx2, cy2))
    els = _ocr_crop_elements(crop, scale=2)
    blobs = _detect_pt_device_blobs(np.array(img), meta, cal)
    return [(e, cal.canvas_x1 + round((e.x / 1000.0) * (cx2 - cx1)), (cal.canvas_y1 + 30) + round((e.y / 1000.0) * (cy2 - cy1))) for e in els], blobs


def _device_family(name: str) -> str:
    low = name.strip().lower()
    if any(k in low for k in ("router", "1941", "2911", "2901", "2811", "4321", "4331", "isr")) or re.match(r"^r\d+$", low):
        return "router"
    if any(k in low for k in ("switch", "2960", "3560", "3650")) or re.match(r"^(?:s|sw)\d+$", low):
        return "switch"
    if any(k in low for k in ("wlc", "2504", "3504", "controller")):
        return "wlc"
    if any(k in low for k in ("lap", "3702", "access point")) or re.match(r"^(?:lap|ap)[-_]?\d*$", low):
        return "ap"
    if any(k in low for k in ("server", "srv", "radius", "snmp", "dhcp", "dns")):
        return "server"
    if "laptop" in low:
        return "laptop"
    if re.match(r"^pc\d*$", low) or "pc" in low or "admin" in low or "host" in low:
        return "pc"
    return ""


def _open_device_dialog(
    desktop: DesktopController,
    cal: PTCalibration,
    target: str,
    norm_x: float | None = None,
    norm_y: float | None = None,
    glide_ms: int = 60,
    canvas_scan: tuple[list[Any], list[tuple[int, int, list[str]]]] | None = None,
) -> dict[str, Any]:
    """Locate a Packet Tracer device on the canvas by name, teal icon blob, or coordinates and open its dialog window."""
    _close_pt_child_windows(desktop)

    t_low = target.strip().lower()
    t_norm = _normalize_ocr_label(t_low)
    t_fam = _device_family(t_low)
    candidates: list[tuple[int, int]] = []
    coord_hint_set: set[tuple[int, int]] = set()

    if norm_x is not None and norm_y is not None:
        pt = cal.canvas_xy(norm_x, norm_y)
        candidates.append(pt)
        coord_hint_set.add(pt)

    cached = _RECENT_PT_NORM.get(t_low)
    if cached is not None:
        pt = cal.canvas_xy(cached[0], cached[1])
        candidates.append(pt)
        coord_hint_set.add(pt)

    exact_label_found = False
    if not candidates:
        if canvas_scan is None:
            canvas_scan = _scan_canvas_devices(desktop, cal)
        ocr_items, blobs = canvas_scan
        els = [it[0] for it in ocr_items]
        hit = _match_canvas_label(els, target)
        if hit is not None:
            exact_label_found = True
            matched_item = next((it for it in ocr_items if it[0] is hit), None)
            if matched_item is not None:
                _, lx, ly = matched_item
                for y_off in (-36, -22, -52):
                    candidates.append((lx, max(cal.canvas_y1 + 36, ly + y_off)))

        for icx, icy, lbls in blobs:
            joined_norm = " ".join(_normalize_ocr_label(l) for l in lbls)
            if t_norm and any(_normalize_ocr_label(l) == t_norm or t_norm in joined_norm for l in lbls):
                exact_label_found = True
                candidates.append((icx, icy))

        hint_px = cal.canvas_xy(norm_x, 500)[0] if norm_x is not None else None
        hint_py = cal.canvas_xy(500, norm_y)[1] if norm_y is not None else None
        remaining = [b for b in blobs if (b[0], b[1]) not in candidates]
        if hint_px is not None or hint_py is not None:
            remaining.sort(
                key=lambda b: (
                    abs(b[0] - hint_px) if hint_px is not None else 0,
                    abs(b[1] - hint_py) if hint_py is not None else 0,
                )
            )
            for icx, icy, _lbls in remaining[:3]:
                candidates.append((icx, icy))
                coord_hint_set.add((icx, icy))
        else:
            row_hint_first = []
            others = []
            for icx, icy, _lbls in remaining:
                if t_fam in ("router", "switch") and icy < (cal.canvas_y1 + cal.canvas_y2) * 0.65:
                    row_hint_first.append((icx, icy))
                elif t_fam in ("pc", "laptop", "server") and icy > (cal.canvas_y1 + cal.canvas_y2) * 0.55:
                    row_hint_first.append((icx, icy))
                else:
                    others.append((icx, icy))
            candidates.extend((row_hint_first + others)[:4])

    tried: list[tuple[int, int]] = []
    exact_match_required = exact_label_found or len(candidates) > 3
    for gx, gy in candidates[:6]:
        if any(abs(gx - tx) < 12 and abs(gy - ty) < 12 for tx, ty in tried):
            continue
        tried.append((gx, gy))
        desktop.click(gx, gy, restore_cursor=False, glide_ms=glide_ms, label=f"Buka {target}", pre_hover_ms=75)
        time.sleep(0.42)
        dlgs = [w for w in _pt_child_windows(desktop) if not _is_pt_hover_tooltip(w)]
        if not dlgs:
            continue
        dlg = dlgs[0]
        actual_title = str(dlg.get("title") or "").strip()
        if actual_title:
            nx_est = max(60.0, min(940.0, (gx - cal.canvas_x1) / max(1, cal.canvas_x2 - cal.canvas_x1) * 1000.0))
            ny_est = max(60.0, min(940.0, (gy - cal.canvas_y1) / max(1, cal.canvas_y2 - cal.canvas_y1) * 1000.0))
            _RECENT_PT_NORM[actual_title.lower()] = (nx_est, ny_est)

        same_family_on_hint = (not exact_label_found) and ((gx, gy) in coord_hint_set) and t_fam and (_device_family(actual_title) == t_fam)
        if actual_title.lower() == t_low or not exact_match_required or _normalize_ocr_label(actual_title) == t_norm or same_family_on_hint:
            wb.focus_window(f'address:{dlg["address"]}')
            time.sleep(0.14)
            return next((w for w in _pt_child_windows(desktop) if w["address"] == dlg["address"]), dlg)

        _close_pt_child_windows(desktop)
        cal = focus_and_calibrate_pt(desktop)

    raise RuntimeError(f"Perangkat '{target}' tidak ditemukan atau jendelanya gagal dibuka di Packet Tracer.")


def _fill_ocr_textbox(
    desktop: DesktopController,
    field_x: int,
    field_y: int,
    value: str,
    glide_ms: int = 55,
    label: str = "Input",
    press_enter: bool = False,
) -> None:
    """Click a Packet Tracer input box, select-all, clear, and type value reliably."""
    desktop.click(field_x, field_y, restore_cursor=False, glide_ms=glide_ms, label=label, pre_hover_ms=45)
    time.sleep(0.08)
    desktop.press_hotkey("ctrl+a")
    time.sleep(0.04)
    desktop.press_hotkey("BackSpace")
    time.sleep(0.04)
    desktop.type_text(str(value), press_enter=press_enter)
    time.sleep(0.08)


def _wait_if_booting(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    cal: PTCalibration | None = None,
    max_rounds: int = 4,
) -> None:
    """If a Packet Tracer Router/Switch/WLC shows 'Device is still booting', advance time until ready."""
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    for _ in range(max_rounds):
        img, meta = _capture_clean_monitor(desktop, monitor)
        bx1 = max(0, wx - meta.origin_x + 40)
        by1 = max(0, wy - meta.origin_y + 40)
        bx2 = min(img.width, wx - meta.origin_x + ww - 20)
        by2 = min(img.height, wy - meta.origin_y + wh - 40)
        txt = " ".join(e.text.lower() for e in _ocr_crop_elements(img.crop((bx1, by1, bx2, by2)), scale=2))
        if "still booting" not in txt:
            return
        if cal is not None:
            wb.focus_window(f'address:{cal.window_address}')
            time.sleep(0.06)
            desktop.press_hotkey("alt+d")
            time.sleep(0.25)
            wb.focus_window(f'address:{dlg["address"]}')
            time.sleep(0.12)
        else:
            time.sleep(0.75)


def _switch_device_tab(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    want_tab: str,
    target_hint: str = "",
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Switch Packet Tracer device window cleanly to 'physical', 'config', 'cli', 'services', or 'desktop'."""
    tab_low = want_tab.strip().lower()
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    img, meta = _capture_clean_monitor(desktop, monitor)
    ox, oy = meta.origin_x, meta.origin_y
    # On Windows framed windows have ~30px titlebar before QTabBar (~y+28..y+75)
    tab_band_h = min(wh, 84)
    crop = img.crop((wx - ox, wy - oy, wx - ox + ww, wy - oy + tab_band_h))
    tab_els = _ocr_crop_elements(crop, scale=3)

    hit = next((e for e in tab_els if tab_low[:4] in e.text.lower()), None)
    cand_xs: list[int] = []
    tab_y = wy + (round((hit.y / 1000.0) * crop.height) if hit is not None else 44)
    if hit is not None:
        cand_xs.append(wx + round((hit.x / 1000.0) * crop.width))

    attrs = next((e for e in tab_els if "attrib" in e.text.lower()), None)
    serv = next((e for e in tab_els if "serv" in e.text.lower()), None)
    t_low = (dlg.get("title") or target_hint or "").lower()
    attr_x = round((attrs.x / 1000.0) * crop.width) if attrs else 0
    is_server = (serv is not None) or (attr_x > 375) or ("server" in t_low or "srv" in t_low)

    if tab_low == "physical":
        cand_xs.extend([wx + 48, wx + 44])
    elif tab_low == "config":
        cand_xs.extend([wx + 110, wx + 114])
    elif tab_low == "cli":
        cand_xs.extend([wx + 157, wx + 170])
    elif tab_low == "services":
        cand_xs.extend([wx + 172, wx + 168, wx + 180])
    else:
        cand_xs.extend([wx + 236, wx + 170] if is_server else [wx + 170, wx + 236])

    seen: set[int] = set()
    for tx in cand_xs:
        if any(abs(tx - s) < 12 for s in seen):
            continue
        seen.add(tx)
        desktop.click(tx, tab_y, restore_cursor=False, glide_ms=glide_ms, label=f"Tab {tab_low.upper()}", pre_hover_ms=45)
        time.sleep(0.38)
        _wait_if_booting(desktop, dlg, monitor, cal=cal)
        img2, meta2 = _capture_clean_monitor(desktop, monitor)
        body = img2.crop((wx - meta2.origin_x, wy - meta2.origin_y, wx - meta2.origin_x + ww, wy - meta2.origin_y + wh))
        body_els = _ocr_crop_elements(body.crop((0, 0, ww, min(wh, 220))), scale=2)
        joined = " ".join(e.text.lower() for e in body_els)
        if tab_low == "cli":
            bot = body.crop((0, max(0, wh - 110), ww, wh))
            bot_txt = " ".join(e.text.lower() for e in _ocr_crop_elements(bot, scale=2))
            if "command line interface" in joined or "paste" in bot_txt:
                return dlg
        elif tab_low == "config":
            if any(k in joined for k in ("global", "settings", "display", "interface", "management")):
                return dlg
        elif tab_low == "services":
            if any(k in joined for k in ("services", "http", "dhcp", "aaa", "tftp", "syslog")):
                return dlg
        elif tab_low == "physical":
            if "zoom in" in joined or "physical" in joined:
                return dlg
        else:
            if any(k in joined for k in ("configuration", "command prompt", "web browser", "dial-up", "wireless")) or (
                "global settings" not in joined and "zoom in" not in joined and "http" not in joined and "projects" not in joined
            ):
                return dlg
    return dlg


def _set_device_display_name(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    display_name: str,
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Rename a Packet Tracer device's canvas Display Name via Config -> Global -> Settings."""
    _switch_device_tab(desktop, dlg, monitor, "config", target_hint=display_name, glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    img, meta = _capture_clean_monitor(desktop, monitor)
    ox, oy = meta.origin_x, meta.origin_y
    side_crop = img.crop((wx - ox, wy - oy + 40, wx - ox + 220, wy - oy + min(wh, 260)))
    side_els = _ocr_crop_elements(side_crop, scale=2)
    set_el = next((e for e in side_els if "setting" in e.text.lower() and "algo" not in e.text.lower()), None)
    if set_el is not None:
        desktop.click(
            wx + round((set_el.x / 1000.0) * 220),
            wy + 40 + round((set_el.y / 1000.0) * (min(wh, 260) - 40)),
            restore_cursor=False,
            glide_ms=glide_ms,
            label="Settings",
            pre_hover_ms=40,
        )
        time.sleep(0.24)
        img, meta = _capture_clean_monitor(desktop, monitor)
        ox, oy = meta.origin_x, meta.origin_y

    right_crop = img.crop((wx - ox + 170, wy - oy + 40, wx - ox + ww, wy - oy + min(wh, 340)))
    r_els = _ocr_crop_elements(right_crop, scale=2)
    dn_el = next(
        (
            e
            for e in r_els
            if 150 < e.y < 360 and any(k in e.text.lower() for k in ("display", "name |", "|router", "|switch", "|pc", "|server"))
        ),
        None,
    )
    fy = wy + 40 + round((dn_el.y / 1000.0) * (min(wh, 340) - 40)) if dn_el is not None else wy + 125
    fx = wx + max(330, round(ww * 0.42))
    _fill_ocr_textbox(desktop, fx, fy, display_name, glide_ms=glide_ms, label="Display Name")
    desktop.press_hotkey("Tab")
    time.sleep(0.10)
    return {"ok": True, "display_name": display_name}


def _configure_wlc_management(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    spec: dict[str, Any],
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Configure Cisco WLC (2504/3504) Management IP/Mask/Gateway/DNS via Config -> Management."""
    _switch_device_tab(desktop, dlg, monitor, "config", target_hint=str(spec.get("target") or ""), glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    img, meta = _capture_clean_monitor(desktop, monitor)
    ox, oy = meta.origin_x, meta.origin_y
    side = _ocr_crop_elements(img.crop((wx - ox, wy - oy + 40, wx - ox + 230, wy - oy + min(wh, 580))), scale=2)
    mgmt = next((e for e in side if "manag" in e.text.lower()), None)
    if mgmt is None:
        return {"ok": False, "error": "Menu Management tidak ditemukan di panel WLC Config."}
    desktop.click(
        wx + round((mgmt.x / 1000.0) * 230),
        wy + 40 + round((mgmt.y / 1000.0) * (min(wh, 580) - 40)),
        restore_cursor=False,
        glide_ms=glide_ms,
        label="WLC Management",
        pre_hover_ms=45,
    )
    time.sleep(0.30)

    img2, meta2 = _capture_clean_monitor(desktop, monitor)
    ox2, oy2 = meta2.origin_x, meta2.origin_y
    panel_h = min(wh, 500) - 40
    panel = img2.crop((wx - ox2 + 210, wy - oy2 + 40, wx - ox2 + ww, wy - oy2 + 40 + panel_h))
    p_els = _ocr_crop_elements(panel, scale=2)

    def _y_match(keys: tuple[str, ...], fallback_rel_y: int) -> int:
        for e in p_els:
            low = e.text.lower()
            if e.x < 520 and any(k in low for k in keys):
                return wy + 40 + round((e.y / 1000.0) * panel_h)
        return wy + fallback_rel_y

    wlc_cfg = spec.get("wlc_management") if isinstance(spec.get("wlc_management"), dict) else spec
    ipv4_val = str(wlc_cfg.get("ipv4") or wlc_cfg.get("ip") or "").strip()
    mask_val = str(wlc_cfg.get("mask") or wlc_cfg.get("subnet") or "").strip()
    gw_val = str(wlc_cfg.get("gateway") or "").strip()
    dns_val = str(wlc_cfg.get("dns") or "").strip()
    field_x = wx + round(ww * 0.58)
    set_map: dict[str, str] = {}
    for fname, val, keys, fb_y in (
        ("ipv4", ipv4_val, ("address", "ess", "192.168", "ipv4"), 120),
        ("mask", mask_val, ("mask", "ask", "255."), 143),
        ("gateway", gw_val, ("gateway", "ateway"), 168),
        ("dns", dns_val, ("dns",), 192),
    ):
        if not val:
            continue
        fy = _y_match(keys, fb_y)
        _fill_ocr_textbox(desktop, field_x, fy, val, glide_ms=glide_ms, label=f"WLC {fname}")
        set_map[fname] = val
    if set_map:
        desktop.press_hotkey("Tab")
        time.sleep(0.10)
    return {"ok": True, "wlc_management": set_map}


def _configure_server_services(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    spec: dict[str, Any],
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Configure Server-PT Services (AAA/RADIUS clients & users, or DHCP server pools)."""
    _switch_device_tab(desktop, dlg, monitor, "services", target_hint=str(spec.get("target") or ""), glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    summary: dict[str, Any] = {}

    def _click_service(name_sub: str) -> None:
        img, meta = _capture_clean_monitor(desktop, monitor)
        ox, oy = meta.origin_x, meta.origin_y
        sh = min(wh, 640) - 45
        side = _ocr_crop_elements(img.crop((wx - ox, wy - oy + 45, wx - ox + 225, wy - oy + 45 + sh)), scale=2)
        hit = next((e for e in side if name_sub in e.text.lower()), None)
        if hit is not None:
            desktop.click(
                wx + round((hit.x / 1000.0) * 225),
                wy + 45 + round((hit.y / 1000.0) * sh),
                restore_cursor=False,
                glide_ms=glide_ms,
                label=f"Service {name_sub.upper()}",
                pre_hover_ms=45,
            )
            time.sleep(0.28)

    aaa_cfg = spec.get("radius_server") or spec.get("aaa_radius")
    if isinstance(aaa_cfg, dict):
        _click_service("aaa")
        img, meta = _capture_clean_monitor(desktop, monitor)
        ox, oy = meta.origin_x, meta.origin_y
        pw, ph = ww - 200, wh - 90
        p_els = _ocr_crop_elements(img.crop((wx - ox + 200, wy - oy + 45, wx - ox + ww, wy - oy + 45 + ph)), scale=2)

        def _px(rel_x: float) -> int:
            return wx + 200 + round((rel_x / 1000.0) * pw)

        def _py(rel_y: float) -> int:
            return wy + 45 + round((rel_y / 1000.0) * ph)

        on_el = next((e for e in p_els if ("on" in e.text.lower() or "off" in e.text.lower()) and e.y < 150), None)
        if aaa_cfg.get("enabled", True):
            desktop.click(_px((on_el.x - 28) if on_el else 225), _py(on_el.y if on_el else 89), restore_cursor=False, glide_ms=glide_ms, label="AAA On", pre_hover_ms=40)
            time.sleep(0.10)

        clients = list(aaa_cfg.get("clients") or [])
        users = list(aaa_cfg.get("users") or [])
        add_btns = sorted((e for e in p_els if e.text.strip().lower() == "add"), key=lambda e: e.y)
        top_add = add_btns[0] if add_btns else None
        bot_add = add_btns[-1] if len(add_btns) > 1 else None

        cname_el = next((e for e in p_els if "name" in e.text.lower() and 130 < e.y < 235), None)
        cip_el = next((e for e in p_els if "client ip" in e.text.lower() and 130 < e.y < 235), None)
        sec_el = next((e for e in p_els if ("secret" in e.text.lower() or e.text.strip() == "3") and 205 < e.y < 270), None)

        clients_added = 0
        for cl in clients:
            cy_row1 = _py(cname_el.y if cname_el else (cip_el.y if cip_el else 185))
            cy_row2 = _py(sec_el.y if sec_el else 230)
            if cl.get("name"):
                _fill_ocr_textbox(desktop, _px(200), cy_row1, str(cl["name"]), glide_ms=glide_ms, label="AAA Client Name")
            if cl.get("ip"):
                _fill_ocr_textbox(desktop, _px(690), cy_row1, str(cl["ip"]), glide_ms=glide_ms, label="AAA Client IP")
            if cl.get("secret"):
                _fill_ocr_textbox(desktop, _px(200), cy_row2, str(cl["secret"]), glide_ms=glide_ms, label="AAA Secret")
            if top_add is not None:
                desktop.click(_px(top_add.x), _py(top_add.y), restore_cursor=False, glide_ms=glide_ms, label="Add RADIUS Client", pre_hover_ms=45)
                time.sleep(0.14)
                clients_added += 1

        uname_el = next((e for e in p_els if ("ame" in e.text.lower() or "user" in e.text.lower()) and e.y > 550), None)
        upass_el = next((e for e in p_els if "password" in e.text.lower() and e.y > 550 and e.x > 350), None)
        users_added = 0
        for usr in users:
            uy = _py(uname_el.y if uname_el else (upass_el.y if upass_el else 715))
            if usr.get("username"):
                _fill_ocr_textbox(desktop, _px(220), uy, str(usr["username"]), glide_ms=glide_ms, label="AAA User")
            if usr.get("password"):
                _fill_ocr_textbox(desktop, _px(680), uy, str(usr["password"]), glide_ms=glide_ms, label="AAA Pass")
            add_u = bot_add or top_add
            if add_u is not None:
                desktop.click(_px(add_u.x), _py(add_u.y), restore_cursor=False, glide_ms=glide_ms, label="Add AAA User", pre_hover_ms=45)
                time.sleep(0.14)
                users_added += 1
        summary["radius_server"] = {"clients_added": clients_added, "users_added": users_added}

    dhcp_cfg = spec.get("dhcp_server")
    if isinstance(dhcp_cfg, dict):
        _click_service("dhcp")
        img, meta = _capture_clean_monitor(desktop, monitor)
        ox, oy = meta.origin_x, meta.origin_y
        pw, ph = ww - 200, wh - 90
        p_els = _ocr_crop_elements(img.crop((wx - ox + 200, wy - oy + 45, wx - ox + ww, wy - oy + 45 + ph)), scale=2)

        def _px2(rel_x: float) -> int:
            return wx + 200 + round((rel_x / 1000.0) * pw)

        def _py2(rel_y: float) -> int:
            return wy + 45 + round((rel_y / 1000.0) * ph)

        on_el = next((e for e in p_els if ("on" in e.text.lower() or "off" in e.text.lower()) and e.y < 140), None)
        if dhcp_cfg.get("enabled", True):
            desktop.click(_px2((on_el.x - 28) if on_el else 225), _py2(on_el.y if on_el else 75), restore_cursor=False, glide_ms=glide_ms, label="DHCP On", pre_hover_ms=40)
            time.sleep(0.10)

        pools = list(dhcp_cfg.get("pools") or ([dhcp_cfg] if any(k in dhcp_cfg for k in ("gateway", "start_ip")) else []))
        pools_saved = 0
        for pool in pools:
            for key, lbl_keys, fb_y in (
                ("name", ("pool name",), 150),
                ("gateway", ("default gateway", "gateway"), 185),
                ("dns", ("dns server", "dns"), 215),
                ("start_ip", ("start ip",), 245),
                ("mask", ("subnet mask",), 275),
                ("max_users", ("maximum number",), 305),
            ):
                val = str(pool.get(key) or "").strip()
                if not val:
                    continue
                lbl = next((e for e in p_els if e.x < 450 and any(k in e.text.lower() for k in lbl_keys)), None)
                fy = _py2(lbl.y if lbl else fb_y)
                _fill_ocr_textbox(desktop, _px2(600), fy, val, glide_ms=glide_ms, label=f"DHCP {key}")
            btn = next((e for e in p_els if e.text.strip().lower() in ("add", "save")), None)
            if btn is not None:
                desktop.click(_px2(btn.x), _py2(btn.y), restore_cursor=False, glide_ms=glide_ms, label="Save DHCP Pool", pre_hover_ms=45)
                time.sleep(0.14)
                pools_saved += 1
        summary["dhcp_server"] = {"pools_saved": pools_saved}

    return {"ok": True, **summary}


def _close_desktop_subpanel(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    glide_ms: int = 55,
) -> None:
    """Locate the blue sub-panel titlebar [X] on PC/Laptop Desktop dynamically and click it."""
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    img, meta = _capture_clean_monitor(desktop, monitor)
    arr = np.array(img)
    ox, oy = meta.origin_x, meta.origin_y
    y1 = max(0, wy + 42 - oy)
    y2 = min(arr.shape[0], wy + min(wh, 125) - oy)
    x1 = max(0, wx - ox)
    x2 = min(arr.shape[1], wx + ww - ox)
    if y2 > y1 and x2 > x1 + 40:
        sub = arr[y1:y2, x1:x2].astype(int)
        blue_mask = (sub[:, :, 2] - sub[:, :, 0] > 20) & (sub[:, :, 2] > 90)
        row_cnt = blue_mask.sum(axis=1)
        best_r = int(np.argmax(row_cnt))
        if int(row_cnt[best_r]) > max(120, ww // 4):
            cols = np.where(blue_mask[best_r])[0]
            if len(cols):
                cx = wx + int(cols[-1]) - 14
                cy = wy + 42 + best_r
                desktop.click(cx, cy, restore_cursor=False, glide_ms=glide_ms, label="Tutup subpanel", pre_hover_ms=40)
                time.sleep(0.35)
                return
    desktop.click(wx + ww - 22, wy + 78, restore_cursor=False, glide_ms=glide_ms, label="Tutup subpanel", pre_hover_ms=40)
    time.sleep(0.35)


def _open_desktop_app(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    app_name: str,
    url: str = "",
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Open a PC/Laptop/Server Desktop utility ('browser', 'wireless', 'command', 'mib') cleanly."""
    _switch_device_tab(desktop, dlg, monitor, "desktop", target_hint=str(dlg.get("title") or ""), glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    img, meta = _capture_clean_monitor(desktop, monitor)
    crop_h = min(wh, 520) - 40
    els = _ocr_crop_elements(img.crop((wx - meta.origin_x, wy - meta.origin_y + 40, wx - meta.origin_x + ww, wy - meta.origin_y + 40 + crop_h)), scale=2)

    low_app = app_name.strip().lower()
    key_map = {
        "browser": ("browser", "web"),
        "wireless": ("wireless",),
        "command": ("command", "prompt"),
        "mib": ("mib",),
    }
    search_keys = key_map.get(low_app, (low_app,))
    lbl = next((e for e in els if any(k in e.text.lower() for k in search_keys)), None)
    if lbl is None:
        _close_desktop_subpanel(desktop, dlg, monitor, glide_ms=glide_ms)
        img, meta = _capture_clean_monitor(desktop, monitor)
        els = _ocr_crop_elements(img.crop((wx - meta.origin_x, wy - meta.origin_y + 40, wx - meta.origin_x + ww, wy - meta.origin_y + 40 + crop_h)), scale=2)
        lbl = next((e for e in els if any(k in e.text.lower() for k in search_keys)), None)
    if lbl is None:
        return {"ok": False, "error": f"Ikon Desktop '{app_name}' tidak ditemukan."}

    ix = wx + round((lbl.x / 1000.0) * ww)
    iy = max(wy + 68, wy + 40 + round((lbl.y / 1000.0) * crop_h) - 52)
    desktop.click(ix, iy, restore_cursor=False, glide_ms=glide_ms, label=f"Desktop {app_name}", pre_hover_ms=55)
    time.sleep(0.65 if low_app == "browser" else 0.45)

    out: dict[str, Any] = {"ok": True, "opened_app": low_app}
    if low_app == "browser" and url.strip():
        img_b, meta_b = _capture_clean_monitor(desktop, monitor)
        top_b = _ocr_crop_elements(img_b.crop((wx - meta_b.origin_x, wy - meta_b.origin_y + 50, wx - meta_b.origin_x + ww, wy - meta_b.origin_y + 180)), scale=2)
        url_lbl = next((e for e in top_b if "url" in e.text.lower()), None)
        ux = wx + round(ww * 0.45)
        uy = wy + 50 + round((url_lbl.y / 1000.0) * 130) if url_lbl is not None else wy + 120
        _fill_ocr_textbox(desktop, ux, uy, url.strip(), glide_ms=glide_ms, label="Browser URL", press_enter=True)
        time.sleep(0.95)
        out["url"] = url.strip()
    return out


def _configure_ios_cli(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    commands: list[str] | str,
    verify_cli: bool = True,
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Safely enter IOS Exec/Config mode in PT CLI, paste commands via PT Paste button, and capture output."""
    _switch_device_tab(desktop, dlg, monitor, "cli", glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]

    def _read_tail() -> list[str]:
        img, meta = _capture_clean_monitor(desktop, monitor)
        x1 = max(0, wx - meta.origin_x + 18)
        y1 = max(0, wy - meta.origin_y + max(55, wh - 320))
        x2 = min(img.width, wx - meta.origin_x + ww - 48)
        y2 = min(img.height, wy - meta.origin_y + wh - 92)
        return _ocr_cli_lines(img.crop((x1, y1, x2, y2)))

    img_b, meta_b = _capture_clean_monitor(desktop, monitor)
    bx1 = max(0, wx - meta_b.origin_x + ww // 2)
    by1 = max(0, wy - meta_b.origin_y + wh - 115)
    bx2 = min(img_b.width, wx - meta_b.origin_x + ww)
    by2 = min(img_b.height, wy - meta_b.origin_y + wh)
    bot_crop = img_b.crop((bx1, by1, bx2, by2))
    paste_els = _ocr_crop_elements(bot_crop, scale=2)
    paste_hit = next((e for e in paste_els if "paste" in e.text.lower()), None)
    if paste_hit is not None:
        px = wx + ww // 2 + round((paste_hit.x / 1000.0) * (bx2 - bx1))
        py = wy + (wh - 115) + round((paste_hit.y / 1000.0) * (by2 - by1))
    else:
        px, py = wx + ww - 64, wy + wh - 76

    def _paste_via_pt_button(text: str, settle_s: float = 0.35) -> None:
        ok_prev, prev_clip, _ = wb.clipboard_get()
        ok_cp, cp_err = wb.clipboard_set(text)
        if not ok_cp:
            raise RuntimeError(f"Gagal salin perintah CLI ke clipboard: {cp_err}")
        time.sleep(0.06)
        try:
            desktop.click(px, py, restore_cursor=False, glide_ms=glide_ms, label="Paste CLI", pre_hover_ms=55)
            time.sleep(settle_s)
        finally:
            if ok_prev and prev_clip:
                wb.clipboard_set(prev_clip)

    def _detect_prompt(tail_lines: list[str]) -> str:
        for ln in reversed(tail_lines[-4:]):
            clean = re.sub(r"[\|\[\]v]+$", "", ln).strip()
            if "#" in clean:
                return clean
            if ">" in clean and not clean.lower().endswith("[yes/no]:"):
                return clean
        return tail_lines[-1] if tail_lines else ""

    desktop.click(wx + ww // 2, wy + wh // 2, restore_cursor=False, glide_ms=glide_ms, label="Fokus CLI", pre_hover_ms=45)
    time.sleep(0.15)
    desktop.press_hotkey("Return")
    time.sleep(0.28)

    last_prompt = ""
    for _ in range(5):
        tail = _read_tail()
        recent = tail[-5:] if tail else []
        joined = "\n".join(recent).lower()
        last_line = tail[-1] if tail else ""
        if "translating" in joined or "255.255.255.255" in joined:
            desktop.press_hotkey("ctrl+shift+6")
            time.sleep(0.45)
            desktop.press_hotkey("Return")
            time.sleep(0.25)
            continue
        if "[yes/no]" in joined or "initial configuration dialog" in joined or "please answer" in joined:
            if not any(re.search(r"[a-zA-Z0-9_-]\s*[>#]", ln) for ln in recent[-2:]):
                _paste_via_pt_button("no\n", settle_s=0.45)
                desktop.press_hotkey("Return")
                time.sleep(0.28)
                continue
        if "--more--" in last_line.lower():
            desktop.press_hotkey("space")
            time.sleep(0.2)
            continue
        last_prompt = _detect_prompt(tail)
        if "#" in last_prompt or ">" in last_prompt:
            break
        desktop.press_hotkey("Return")
        time.sleep(0.28)

    if last_prompt and "#" not in last_prompt:
        _paste_via_pt_button("enable\n", settle_s=0.25)
        desktop.press_hotkey("Return")
        time.sleep(0.22)
        tail = _read_tail()
        last_prompt = _detect_prompt(tail)

    script_lines = _normalize_ios_script(commands, current_prompt=last_prompt, verify_cli=verify_cli)
    if not script_lines:
        return {"ok": True, "cli_tail": _read_tail()[-12:], "cli_errors": []}

    split_idx = len(script_lines)
    for i in range(len(script_lines) - 1, -1, -1):
        low_i = script_lines[i].strip().lower()
        if low_i.startswith("show ") or low_i == "terminal length 0":
            split_idx = i
        else:
            break
    main_lines = script_lines[:split_idx]
    show_lines = script_lines[split_idx:]

    if main_lines:
        _paste_via_pt_button("\n".join(main_lines) + "\n", settle_s=min(2.2, 0.35 + 0.04 * len(main_lines)))
        desktop.press_hotkey("Return")
        time.sleep(0.22)
    for sl in show_lines:
        _paste_via_pt_button(sl + "\n", settle_s=0.40)
        if sl.strip().lower().startswith("show "):
            for _ in range(6):
                t_chk = _read_tail()
                if t_chk and "--more--" in t_chk[-1].lower():
                    desktop.press_hotkey("space")
                    time.sleep(0.22)
                else:
                    break
        else:
            time.sleep(0.12)

    tail = _read_tail()
    scan_lines = tail
    anchor_src = main_lines[0] if main_lines else show_lines[0]
    anchor = anchor_src.strip().lower()[:18]
    if anchor:
        for i in range(len(tail) - 1, -1, -1):
            if anchor in tail[i].lower():
                scan_lines = tail[i:]
                break

    err_kws = ("% invalid", "% incomplete", "% ambiguous", "% bad", "overlap", "translating", "% please answer")
    cli_errors = [ln for ln in scan_lines if any(k in ln.lower() for k in err_kws)]
    return {
        "ok": len(cli_errors) == 0,
        "commands_sent": len(script_lines),
        "cli_errors": cli_errors,
        "cli_tail": tail[-14:],
    }


def _configure_desktop_host(
    desktop: DesktopController,
    dlg: dict[str, Any],
    monitor: str,
    spec: dict[str, Any],
    glide_ms: int = 55,
    cal: PTCalibration | None = None,
) -> dict[str, Any]:
    """Configure PC/Laptop/Server IPv4/Mask/Gateway/DNS or DHCP and optionally run Command Prompt checks."""
    _switch_device_tab(desktop, dlg, monitor, "desktop", target_hint=str(spec.get("target") or ""), glide_ms=glide_ms, cal=cal)
    wx, wy = dlg["at"]
    ww, wh = dlg["size"]
    result: dict[str, Any] = {"ok": True}

    wants_ip = any(spec.get(k) is not None for k in ("ipv4", "ip", "mask", "subnet", "gateway", "dns", "dhcp"))
    if wants_ip:
        img_d, meta_d = _capture_clean_monitor(desktop, monitor)
        crop_d = img_d.crop((wx - meta_d.origin_x, wy - meta_d.origin_y, wx - meta_d.origin_x + ww, wy - meta_d.origin_y + min(wh, 360)))
        els_d = _ocr_crop_elements(crop_d, scale=2)
        ip_lbl = next((e for e in els_d if "conf" in e.text.lower()), None)
        if ip_lbl is not None and ip_lbl.y > 400:
            ix = wx + round((ip_lbl.x / 1000.0) * ww)
            iy = max(wy + 78, min(wy + 165, wy + round((ip_lbl.y / 1000.0) * min(wh, 360)) - 75))
        else:
            ix, iy = wx + 110, wy + 125
        desktop.click(ix, iy, restore_cursor=False, glide_ms=glide_ms, label="IP Config", pre_hover_ms=65)
        time.sleep(0.45)

        img_ip, meta_ip = _capture_clean_monitor(desktop, monitor)
        top_ip = img_ip.crop((wx - meta_ip.origin_x, wy - meta_ip.origin_y, wx - meta_ip.origin_x + ww, wy - meta_ip.origin_y + min(wh, 360)))
        els_ip = _ocr_crop_elements(top_ip, scale=2)

        def _y_for(keys: tuple[str, ...], fallback_y: int) -> int:
            for e in els_ip:
                low = e.text.lower()
                if e.x < 320 and any(k in low for k in keys):
                    return round((e.y / 1000.0) * min(wh, 360))
            return fallback_y

        use_dhcp = bool(spec.get("dhcp", False))
        if use_dhcp:
            dhcp_el = next((e for e in els_ip if "dhcp" in e.text.lower() and e.y < 520), None)
            dx = wx + (round((dhcp_el.x / 1000.0) * ww) - 18 if dhcp_el else 44)
            dy = wy + (round((dhcp_el.y / 1000.0) * min(wh, 360)) if dhcp_el else 155)
            desktop.click(dx, dy, restore_cursor=False, glide_ms=glide_ms, label="Pilih DHCP", pre_hover_ms=50)
            time.sleep(1.7)
            img_after, meta_after = _capture_clean_monitor(desktop, monitor)
            after_top = img_after.crop((wx - meta_after.origin_x, wy - meta_after.origin_y, wx - meta_after.origin_x + ww, wy - meta_after.origin_y + 320))
            val_lines = _ocr_cli_lines(after_top)
            result["dhcp"] = True
            result["ip_summary"] = val_lines[:10]
        else:
            stat_el = next((e for e in els_ip if "static" in e.text.lower() and (e.y / 1000.0 * min(wh, 360)) < 195), None)
            sx = wx + (round((stat_el.x / 1000.0) * ww) - 18 if stat_el else 334)
            sy = wy + (round((stat_el.y / 1000.0) * min(wh, 360)) if stat_el else 155)
            desktop.click(sx, sy, restore_cursor=False, glide_ms=glide_ms, label="Pilih Static", pre_hover_ms=45)
            time.sleep(0.14)

            field_x = wx + round(ww * 0.58)
            ipv4_val = str(spec.get("ipv4") or spec.get("ip") or "").strip()
            mask_val = str(spec.get("mask") or spec.get("subnet") or "").strip()
            gw_val = str(spec.get("gateway") or "").strip()
            dns_val = str(spec.get("dns") or "").strip()

            fields = [
                ("ipv4", ipv4_val, ("ipv4", "1pv4", "address"), 186),
                ("mask", mask_val, ("subnet", "mask"), 215),
                ("gateway", gw_val, ("gateway",), 245),
                ("dns", dns_val, ("dns",), 273),
            ]
            set_map: dict[str, str] = {}
            for fname, val, keys, fb_y in fields:
                if not val:
                    continue
                fy = wy + _y_for(keys, fb_y)
                _fill_ocr_textbox(desktop, field_x, fy, val, glide_ms=glide_ms, label=f"Isi {fname}")
                set_map[fname] = val
            if set_map:
                desktop.press_hotkey("Tab")
                time.sleep(0.10)
            result["ip_configured"] = set_map

    term_cmds = spec.get("terminal_commands")
    if term_cmds:
        if wants_ip:
            _close_desktop_subpanel(desktop, dlg, monitor, glide_ms=glide_ms)
        img_d2, meta_d2 = _capture_clean_monitor(desktop, monitor)
        crop_d2 = img_d2.crop((wx - meta_d2.origin_x, wy - meta_d2.origin_y, wx - meta_d2.origin_x + ww, wy - meta_d2.origin_y + min(wh, 360)))
        els_d2 = _ocr_crop_elements(crop_d2, scale=2)
        cmd_el = next((e for e in els_d2 if "command" in e.text.lower() or "prompt" in e.text.lower()), None)
        if cmd_el is not None and cmd_el.y > 400:
            cx = wx + round((cmd_el.x / 1000.0) * ww)
            cy = max(wy + 78, min(wy + 165, wy + round((cmd_el.y / 1000.0) * min(wh, 360)) - 72))
        else:
            cx, cy = wx + 638, wy + 125
        desktop.click(cx, cy, restore_cursor=False, glide_ms=glide_ms, label="Command Prompt", pre_hover_ms=65)
        time.sleep(0.45)

        cmd_list = [term_cmds] if isinstance(term_cmds, str) else [str(c) for c in term_cmds]
        desktop.click(wx + ww // 2, wy + wh // 2, restore_cursor=False, glide_ms=glide_ms, label="Fokus CMD", pre_hover_ms=45)
        time.sleep(0.15)
        for cmd in cmd_list[:8]:
            desktop.type_text(cmd, press_enter=True, direct_type=True)
            low_c = cmd.lower()
            time.sleep(3.2 if ("ping" in low_c or "tracert" in low_c) else 0.5)
        img_t, meta_t = _capture_clean_monitor(desktop, monitor)
        t_crop = img_t.crop((wx - meta_t.origin_x + 16, wy - meta_t.origin_y + 72, wx - meta_t.origin_x + ww - 22, wy - meta_t.origin_y + wh - 50))
        result["terminal_output"] = _ocr_cli_lines(t_crop)[-18:]

    return result


def configure_packet_tracer_devices(
    desktop: DesktopController,
    devices: list[dict[str, Any]],
    stop_event: threading.Event,
    pause_gate: Any,
    glide_ms: int = 65,
    restore_cursor: bool = True,
) -> dict[str, Any]:
    """Configure PT Routers/Switches (CLI/STP/VLAN/Trunk/EtherChannel), WLC Management, Server AAA/DHCP, and Hosts."""
    cal = focus_and_calibrate_pt(desktop)
    step_glide = min(glide_ms, 70)
    results: list[dict[str, Any]] = []
    canvas_scan: tuple[list[Any], list[tuple[int, int, list[str]]]] | None = None
    kept_open = False

    _shield = desktop.physical_cursor_shield()
    _shield.__enter__()
    try:
        for spec in devices[:20]:
            pause_gate()
            if stop_event.is_set():
                break
            target = str(spec.get("target") or spec.get("name") or "").strip()
            if not target:
                results.append({"target": "", "ok": False, "error": "Nama target kosong."})
                continue

            norm_x = float(spec["x"]) if spec.get("x") is not None else None
            norm_y = float(spec["y"]) if spec.get("y") is not None else None
            has_cli = bool(spec.get("cli_commands") or spec.get("commands"))
            has_services = isinstance(spec.get("radius_server") or spec.get("aaa_radius"), dict) or isinstance(spec.get("dhcp_server"), dict)
            has_host_cfg = any(spec.get(k) is not None for k in ("ipv4", "ip", "mask", "subnet", "gateway", "dns", "dhcp", "terminal_commands"))
            display_name = str(spec.get("display_name") or "").strip()
            open_app = str(spec.get("open_app") or "").strip().lower()
            keep_this_open = bool(spec.get("keep_open", False) or open_app)

            if norm_x is None and _RECENT_PT_NORM.get(target.lower()) is None and canvas_scan is None:
                canvas_scan = _scan_canvas_devices(desktop, cal)

            try:
                dlg = _open_device_dialog(desktop, cal, target, norm_x=norm_x, norm_y=norm_y, glide_ms=step_glide, canvas_scan=canvas_scan)
                actual_title = str(dlg.get("title") or target)
                fam = _device_family(actual_title) or _device_family(target)
                dev_res: dict[str, Any] = {"target": target, "ok": True}

                if display_name:
                    dn_res = _set_device_display_name(desktop, dlg, cal.monitor, display_name, glide_ms=step_glide, cal=cal)
                    dev_res.update(dn_res)
                    if _RECENT_PT_NORM.get(target.lower()) is not None:
                        _RECENT_PT_NORM[display_name.lower()] = _RECENT_PT_NORM[target.lower()]

                if has_cli:
                    cmds = spec.get("cli_commands") if spec.get("cli_commands") is not None else spec.get("commands")
                    verify_cli = bool(spec.get("verify_cli", True))
                    cli_r = _configure_ios_cli(desktop, dlg, cal.monitor, cmds, verify_cli=verify_cli, glide_ms=step_glide, cal=cal)
                    dev_res["mode"] = "cli"
                    dev_res.update(cli_r)
                elif fam == "wlc" or isinstance(spec.get("wlc_management"), dict):
                    if isinstance(spec.get("wlc_management"), dict) or has_host_cfg:
                        wlc_r = _configure_wlc_management(desktop, dlg, cal.monitor, spec, glide_ms=step_glide, cal=cal)
                        dev_res["mode"] = "wlc_management"
                        dev_res.update(wlc_r)
                else:
                    if has_host_cfg:
                        host_r = _configure_desktop_host(desktop, dlg, cal.monitor, spec, glide_ms=step_glide, cal=cal)
                        dev_res["mode"] = "desktop"
                        dev_res.update(host_r)
                    if has_services:
                        srv_r = _configure_server_services(desktop, dlg, cal.monitor, spec, glide_ms=step_glide, cal=cal)
                        dev_res["mode"] = "services" if not has_host_cfg else "desktop+services"
                        dev_res.update(srv_r)

                if open_app:
                    if open_app in ("cli", "config", "services", "desktop", "physical"):
                        _switch_device_tab(desktop, dlg, cal.monitor, open_app, target_hint=actual_title, glide_ms=step_glide, cal=cal)
                        dev_res["opened_tab"] = open_app
                    else:
                        app_r = _open_desktop_app(
                            desktop,
                            dlg,
                            cal.monitor,
                            open_app,
                            url=str(spec.get("browser_url") or spec.get("url") or ""),
                            glide_ms=step_glide,
                            cal=cal,
                        )
                        dev_res.update(app_r)

                results.append(dev_res)
                if keep_this_open:
                    kept_open = True
                    break
            except Exception as exc:
                results.append({"target": target, "ok": False, "error": str(exc)})
            finally:
                if not kept_open:
                    _close_pt_child_windows(desktop)
                    wb.focus_window(f'address:{cal.window_address}')
                    time.sleep(0.10)
    finally:
        if not kept_open:
            try:
                _close_pt_child_windows(desktop)
            except Exception:
                pass
            try:
                disarm_pt_tools(desktop, cal)
            except Exception:
                pass
        if restore_cursor and desktop._task_orig_cursor is not None:
            try:
                desktop._restore_physical(*desktop._task_orig_cursor)
            except Exception:
                pass
        _shield.__exit__(None, None, None)

    return {
        "ok": all(bool(r.get("ok", False)) for r in results),
        "monitor": cal.monitor,
        "kept_open": kept_open,
        "devices_configured": results,
    }
