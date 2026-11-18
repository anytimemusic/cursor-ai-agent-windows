from __future__ import annotations

import base64
import io
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from PIL import Image, ImageDraw, ImageFont

from . import win32_backend as wb
from .perception import UiElement, draw_som_marks, scan_ui_elements


@dataclass
class ScreenshotMeta:
    monitor: str
    origin_x: int
    origin_y: int
    logical_w: int
    logical_h: int
    img_w: int
    img_h: int


def _draw_coord_grid(
    im: Image.Image,
    coord_origin_x: float = 0.0,
    coord_origin_y: float = 0.0,
    px_per_coord_x: float = 1.0,
    px_per_coord_y: float = 1.0,
    step: int = 100,
    major_step: int = 200,
    last_click: tuple[float, float] | None = None,
) -> Image.Image:
    """Draw a coordinate grid, non-overlapping top/right ruler labels, and last-click crosshair."""
    base = im.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    w, h = base.size

    min_cx = int(coord_origin_x // step) * step
    max_cx = int((coord_origin_x + w / max(1e-6, px_per_coord_x)) // step + 1) * step
    min_cy = int(coord_origin_y // step) * step
    max_cy = int((coord_origin_y + h / max(1e-6, px_per_coord_y)) // step + 1) * step

    for cx in range(min_cx, max_cx + 1, step):
        px = round((cx - coord_origin_x) * px_per_coord_x)
        if 0 <= px < w:
            is_major = cx % major_step == 0
            col = (56, 189, 248, 85) if is_major else (148, 163, 184, 34)
            draw.line([(px, 0), (px, h)], fill=col, width=1)
            if is_major and 20 <= px <= w - 26:
                lbl = str(cx)
                tw = len(lbl) * 6
                bx = max(0, min(w - tw - 4, px - tw // 2 - 2))
                draw.rectangle([bx, 1, bx + tw + 4, 13], fill=(9, 13, 22, 190))
                draw.text((bx + 2, 2), lbl, fill=(186, 230, 253, 245), font=font)

    for cy in range(min_cy, max_cy + 1, step):
        py = round((cy - coord_origin_y) * px_per_coord_y)
        if 0 <= py < h:
            is_major = cy % major_step == 0
            col = (56, 189, 248, 85) if is_major else (148, 163, 184, 34)
            draw.line([(0, py), (w, py)], fill=col, width=1)
            if is_major and 18 <= py <= h - 18:
                lbl = str(cy)
                tw = len(lbl) * 6
                by = max(14, min(h - 13, py - 5))
                draw.rectangle([w - tw - 6, by, w - 1, by + 11], fill=(9, 13, 22, 190))
                draw.text((w - tw - 4, by + 1), lbl, fill=(186, 230, 253, 245), font=font)

    if last_click is not None:
        lcx = round((last_click[0] - coord_origin_x) * px_per_coord_x)
        lcy = round((last_click[1] - coord_origin_y) * px_per_coord_y)
        if 0 <= lcx < w and 0 <= lcy < h:
            draw.ellipse([lcx - 8, lcy - 8, lcx + 8, lcy + 8], outline=(244, 63, 94, 240), width=2)
            draw.line([(lcx - 13, lcy), (lcx + 13, lcy)], fill=(244, 63, 94, 235), width=2)
            draw.line([(lcx, lcy - 13), (lcx, lcy + 13)], fill=(244, 63, 94, 235), width=2)

    return Image.alpha_composite(base, overlay).convert("RGB")


class DesktopController:
    def __init__(self, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.on_event = on_event
        self.ai_cursor_x: int = 960
        self.ai_cursor_y: int = 540
        self.last_shot: ScreenshotMeta | None = None
        self.last_elements: list[UiElement] = []
        self.last_elements_monitor: str = ""
        self.last_click_global: tuple[int, int] | None = None
        self._in_task: bool = False
        self._task_orig_cursor: tuple[int, int] | None = None
        self._task_did_input: bool = False
        self._shield_lock = threading.RLock()
        self._shield_depth: int = 0
        self._input_shield_depth: int = 0
        self._os_cursor_hidden: bool = False
        self._input_txn_lock = threading.RLock()
        self._capture_lock = threading.Lock()
        self._init_cursor_pos()

    def set_os_cursor_hidden(self, hidden: bool) -> None:
        """Track physical cursor shield state and notify overlay cleanly."""
        with self._shield_lock:
            target = bool(hidden)
            if self._os_cursor_hidden == target:
                return
            self._os_cursor_hidden = target
            self.emit({"type": "os_cursor_hidden", "hidden": target})

    def reset_physical_shield(self) -> None:
        with self._shield_lock:
            self._shield_depth = 0
            if self._input_shield_depth > 0:
                self._input_shield_depth = 0
                self.emit({"type": "input_shield", "active": False})
            self.set_os_cursor_hidden(False)

    @contextmanager
    def overlay_input_shield(self) -> Iterator[None]:
        """Make AI cursor overlay click-through while synthetic pointer actions execute."""
        with self._shield_lock:
            outer = self._input_shield_depth == 0
            self._input_shield_depth += 1
            if outer:
                self.emit({"type": "input_shield", "active": True})
        try:
            yield
        finally:
            with self._shield_lock:
                self._input_shield_depth = max(0, self._input_shield_depth - 1)
                if self._input_shield_depth == 0:
                    self.emit({"type": "input_shield", "active": False})

    @contextmanager
    def physical_cursor_shield(self, restore_to: tuple[int, int] | None = None) -> Iterator[None]:
        """Keep user physical pointer restored cleanly around AI pointer actions."""
        with self._shield_lock:
            outer = self._shield_depth == 0
            self._shield_depth += 1
            if outer:
                self.set_os_cursor_hidden(True)
        try:
            yield
        finally:
            with self._shield_lock:
                self._shield_depth = max(0, self._shield_depth - 1)
                if self._shield_depth == 0:
                    if restore_to is not None:
                        try:
                            self._restore_physical(*restore_to)
                        except Exception:
                            pass
                    self.set_os_cursor_hidden(False)

    def _mark_did_input(self) -> None:
        self._task_did_input = True

    def _init_cursor_pos(self) -> None:
        try:
            mons = self.get_monitors()
            focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
            if focused:
                self.ai_cursor_x = int(focused["x"]) + int(focused["logical_w"]) // 2
                self.ai_cursor_y = int(focused["y"]) + int(focused["logical_h"]) // 2
        except Exception:
            pass

    def begin_task(self) -> None:
        self._in_task = True
        self._task_did_input = False
        try:
            self._task_orig_cursor = self.get_cursor_pos()
        except Exception:
            self._task_orig_cursor = None

    def disarm_if_packet_tracer(self) -> bool:
        """If Packet Tracer's main canvas window is foreground, press Escape twice so cable mode never lingers."""
        try:
            aw = self.get_active_window()
        except Exception:
            return False
        cls = str(aw.get("class") or "").lower()
        title = str(aw.get("title") or "").lower()
        if "packettracer" in cls and title.startswith("cisco packet tracer") and not aw.get("floating"):
            try:
                self.press_hotkey("Escape")
                time.sleep(0.05)
                self.press_hotkey("Escape")
                return True
            except Exception:
                return False
        return False

    def end_task(self, restore_cursor: bool = True, aborted: bool = False) -> None:
        try:
            if aborted and self._task_did_input:
                self.disarm_if_packet_tracer()
            if restore_cursor and self._task_did_input and self._task_orig_cursor is not None:
                self._restore_physical(*self._task_orig_cursor)
        except Exception:
            pass
        finally:
            self.reset_physical_shield()
            self._in_task = False
            self._task_did_input = False
            self._task_orig_cursor = None

    def emit(self, ev: dict[str, Any]) -> None:
        try:
            self.on_event(ev)
        except Exception:
            pass

    def get_monitors(self) -> list[dict[str, Any]]:
        return wb.get_monitors()

    def get_layout_bounds(self) -> tuple[int, int, int, int]:
        mons = self.get_monitors()
        if not mons:
            return 0, 0, 1920, 1080
        min_x = min(int(m["x"]) for m in mons)
        min_y = min(int(m["y"]) for m in mons)
        max_x = max(int(m["x"]) + int(m["logical_w"]) for m in mons)
        max_y = max(int(m["y"]) + int(m["logical_h"]) for m in mons)
        return min_x, min_y, max(1, max_x - min_x), max(1, max_y - min_y)

    def get_cursor_pos(self) -> tuple[int, int]:
        if wb.IS_WINDOWS:
            return wb.get_cursor_pos()
        return self.ai_cursor_x, self.ai_cursor_y

    def get_active_window(self) -> dict[str, Any]:
        return wb.get_active_window()

    def get_windows(self) -> list[dict[str, Any]]:
        return wb.get_windows()

    def build_screen_context(
        self,
        meta: ScreenshotMeta,
        elements: list[UiElement] | None = None,
    ) -> dict[str, Any]:
        all_wins = self.get_windows()
        ctx: dict[str, Any] = {
            "screenshot_monitor": meta.monitor,
            "image_size": [meta.img_w, meta.img_h],
            "monitor_origin_global": [meta.origin_x, meta.origin_y],
            "monitor_logical_size": [meta.logical_w, meta.logical_h],
            "ai_cursor_global": [self.ai_cursor_x, self.ai_cursor_y],
            "active_window": self.get_active_window(),
            "monitors": self.get_monitors(),
            "visible_windows": [w for w in all_wins if w.get("visible_now")][:20],
            "all_windows": all_wins[:30],
            "note": (
                "Grid & top/right ruler use 0..1000 normalized coordinates across this monitor. "
                "Prefer click_element(tag_id/text) for labeled controls in ui_elements, run_macro for multi-step sequences, "
                "or cursor_click/cursor_move with x,y (0..1000, default coord_mode='normalized'). Red crosshair marks last click."
            ),
        }
        if elements:
            ctx["ui_elements"] = [el.to_compact_dict() for el in elements]
        return ctx

    def _capture_monitor_image(self, monitor: str = "focused") -> tuple[Image.Image, ScreenshotMeta]:
        mons = self.get_monitors()
        if not mons:
            raise RuntimeError("No monitors reported by Windows.")

        target = None
        if monitor == "all":
            min_x, min_y, cw, ch = self.get_layout_bounds()
            mon_label = "all"
            orig_x, orig_y, lw, lh = min_x, min_y, cw, ch
        else:
            if monitor == "focused":
                target = next((m for m in mons if m.get("focused")), mons[0])
            else:
                target = next((m for m in mons if str(m.get("name", "")).lower() == monitor.lower()), None)
                if target is None:
                    target = next((m for m in mons if m.get("focused")), mons[0])
            mon_label = str(target["name"])
            orig_x = int(target["x"])
            orig_y = int(target["y"])
            lw = int(target["logical_w"])
            lh = int(target["logical_h"])

        try:
            with self._capture_lock:
                self.emit({"type": "capture_hide", "hidden": True})
                time.sleep(0.045)
                rgb = wb.capture_region_mss(orig_x, orig_y, lw, lh)
        finally:
            self.emit({"type": "capture_hide", "hidden": False})

        meta = ScreenshotMeta(
            monitor=mon_label,
            origin_x=orig_x,
            origin_y=orig_y,
            logical_w=lw,
            logical_h=lh,
            img_w=rgb.width,
            img_h=rgb.height,
        )
        return rgb, meta

    def scan_ui(self, monitor: str = "focused") -> tuple[ScreenshotMeta, list[UiElement]]:
        rgb, meta = self._capture_monitor_image(monitor)
        meta.img_w = meta.logical_w
        meta.img_h = meta.logical_h
        self.last_shot = meta
        elements = scan_ui_elements(rgb, meta.origin_x, meta.origin_y, meta.logical_w, meta.logical_h)
        self.last_elements = elements
        self.last_elements_monitor = meta.monitor
        return meta, elements

    def scan_and_capture(
        self,
        monitor: str = "focused",
        max_width: int = 1440,
        draw_grid: bool = True,
        draw_som: bool = True,
    ) -> tuple[str, ScreenshotMeta, list[UiElement]]:
        rgb, meta = self._capture_monitor_image(monitor)
        elements: list[UiElement] = []
        if draw_som:
            elements = scan_ui_elements(rgb, meta.origin_x, meta.origin_y, meta.logical_w, meta.logical_h)
            self.last_elements = elements
            self.last_elements_monitor = meta.monitor

        if max_width > 0 and rgb.width > max_width:
            new_h = max(1, round(rgb.height * (max_width / rgb.width)))
            rgb = rgb.resize((max_width, new_h), Image.Resampling.LANCZOS)

        img_w, img_h = rgb.size
        meta.img_w = img_w
        meta.img_h = img_h

        if draw_grid:
            lc = None
            if self.last_click_global is not None:
                lx_g, ly_g = self.last_click_global
                if meta.origin_x <= lx_g < meta.origin_x + meta.logical_w and meta.origin_y <= ly_g < meta.origin_y + meta.logical_h:
                    lc = (
                        (lx_g - meta.origin_x) * (1000.0 / max(1, meta.logical_w)),
                        (ly_g - meta.origin_y) * (1000.0 / max(1, meta.logical_h)),
                    )
            rgb = _draw_coord_grid(
                rgb,
                coord_origin_x=0.0,
                coord_origin_y=0.0,
                px_per_coord_x=img_w / 1000.0,
                px_per_coord_y=img_h / 1000.0,
                step=100,
                major_step=200,
                last_click=lc,
            )

        if draw_som and elements:
            rgb = draw_som_marks(rgb, elements)

        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=84, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        self.last_shot = meta
        return f"data:image/jpeg;base64,{b64}", meta, elements

    def take_screenshot(
        self,
        monitor: str = "focused",
        max_width: int = 1440,
        draw_grid: bool = True,
        draw_som: bool = False,
    ) -> tuple[str, ScreenshotMeta]:
        data_url, meta, _ = self.scan_and_capture(
            monitor=monitor,
            max_width=max_width,
            draw_grid=draw_grid,
            draw_som=draw_som,
        )
        return data_url, meta

    def resolve_coords(self, x: float, y: float, coord_mode: str = "normalized") -> tuple[int, int]:
        mode = (coord_mode or "normalized").lower().strip()
        if mode == "global":
            return round(x), round(y)
        if self.last_shot is None:
            mons = self.get_monitors()
            focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
            if focused:
                if mode == "normalized":
                    nx = x / 1000.0 if (x > 1.0 or y > 1.0 or (x == 0 and y == 0)) else x
                    ny = y / 1000.0 if (x > 1.0 or y > 1.0 or (x == 0 and y == 0)) else y
                    return round(int(focused["x"]) + nx * int(focused["logical_w"])), round(int(focused["y"]) + ny * int(focused["logical_h"]))
                if 0 <= x <= focused["logical_w"] and 0 <= y <= focused["logical_h"]:
                    return round(int(focused["x"]) + x), round(int(focused["y"]) + y)
            return round(x), round(y)
        meta = self.last_shot
        if mode == "normalized":
            nx = x / 1000.0 if (x > 1.0 or y > 1.0 or (x == 0 and y == 0)) else x
            ny = y / 1000.0 if (x > 1.0 or y > 1.0 or (x == 0 and y == 0)) else y
            gx = meta.origin_x + nx * meta.logical_w
            gy = meta.origin_y + ny * meta.logical_h
            return round(gx), round(gy)
        gx = meta.origin_x + (x / max(1, meta.img_w)) * meta.logical_w
        gy = meta.origin_y + (y / max(1, meta.img_h)) * meta.logical_h
        return round(gx), round(gy)

    def _restore_physical(self, orig_x: int, orig_y: int) -> None:
        wb.move_pointer(orig_x, orig_y)

    def move_physical_pointer(self, gx: int, gy: int) -> None:
        with self._input_txn_lock:
            self._mark_did_input()
            wb.move_pointer(int(gx), int(gy))

    def _smooth_approach(self, gx: int, gy: int, hover_ms: int) -> None:
        if hover_ms <= 0:
            return
        cur_x, cur_y = self.get_cursor_pos()
        if abs(gx - cur_x) + abs(gy - cur_y) > 20:
            mx = round(cur_x + (gx - cur_x) * 0.85)
            my = round(cur_y + (gy - cur_y) * 0.85)
        else:
            mx, my = gx - 12, gy - 8
        wb.move_pointer(mx, my)
        time.sleep(0.025)
        wb.move_pointer(gx, gy)
        time.sleep(min(0.25, max(0.03, hover_ms / 1000.0)))

    def move_ai_cursor(
        self,
        gx: int,
        gy: int,
        label: str = "",
        glide_ms: int = 110,
        move_physical: bool = False,
    ) -> tuple[int, int]:
        old_x, old_y = self.ai_cursor_x, self.ai_cursor_y
        tx, ty = int(gx), int(gy)
        dx, dy = tx - old_x, ty - old_y
        dist = (dx * dx + dy * dy) ** 0.5

        if dist >= 22 and glide_ms > 0 and not move_physical:
            arc_sign = -1.0 if (old_x + old_y) % 2 == 0 else 1.0
            arc = arc_sign * max(12.0, min(78.0, dist * 0.11))
            nx = -dy / dist
            ny = dx / dist
            cx = (old_x + tx) * 0.5 + nx * arc
            cy = (old_y + ty) * 0.5 + ny * arc

            steps = 3 if dist < 140 else (5 if dist < 440 else 7)
            budget = min(0.24, max(0.075, (glide_ms / 1000.0) * (0.75 + min(1.0, dist / 650.0) * 0.65)))
            step_sleep = budget / steps

            for i in range(1, steps):
                u = i / steps
                t = u * u * (3.0 - 2.0 * u)
                omt = 1.0 - t
                px = round(omt * omt * old_x + 2.0 * omt * t * cx + t * t * tx)
                py = round(omt * omt * old_y + 2.0 * omt * t * cy + t * t * ty)
                self.emit({"type": "cursor_move", "x": px, "y": py, "label": label})
                time.sleep(step_sleep)

        self.ai_cursor_x = tx
        self.ai_cursor_y = ty
        self.emit({"type": "cursor_move", "x": self.ai_cursor_x, "y": self.ai_cursor_y, "label": label})
        if move_physical:
            with self._input_txn_lock:
                self._mark_did_input()
                wb.move_pointer(self.ai_cursor_x, self.ai_cursor_y)
        if glide_ms > 0 and (dist < 22 or move_physical):
            time.sleep(min(0.16, max(0.04, glide_ms / 1000.0)))
        elif glide_ms > 0:
            time.sleep(0.025)
        return self.ai_cursor_x, self.ai_cursor_y

    def click(
        self,
        gx: int,
        gy: int,
        button: str = "left",
        clicks: int = 1,
        restore_cursor: bool = True,
        glide_ms: int = 110,
        label: str = "",
        pre_hover_ms: int = 0,
    ) -> dict[str, Any]:
        btn = button if button in ("left", "right", "middle") else "left"
        cnt = max(1, min(3, int(clicks)))
        with self.overlay_input_shield():
            self.move_ai_cursor(gx, gy, label=label or f"Klik ({btn})", glide_ms=glide_ms, move_physical=False)
            self.emit({"type": "cursor_action", "action": "click", "button": btn, "x": gx, "y": gy})

            with self._input_txn_lock:
                orig_x, orig_y = self.get_cursor_pos()
                self._mark_did_input()
                self.last_click_global = (gx, gy)
                if restore_cursor:
                    with self.physical_cursor_shield(restore_to=(orig_x, orig_y)):
                        if pre_hover_ms > 0:
                            self._smooth_approach(gx, gy, pre_hover_ms)
                        wb.click_pointer(gx, gy, btn, cnt)
                        time.sleep(0.05)
                else:
                    if pre_hover_ms > 0:
                        self._smooth_approach(gx, gy, pre_hover_ms)
                    wb.click_pointer(gx, gy, btn, cnt)
                    time.sleep(0.05)
        return {"x": gx, "y": gy, "button": btn, "clicks": cnt}

    def drag(
        self,
        gx1: int,
        gy1: int,
        gx2: int,
        gy2: int,
        button: str = "left",
        restore_cursor: bool = True,
        glide_ms: int = 240,
    ) -> dict[str, Any]:
        btn = button if button in ("left", "right", "middle") else "left"
        with self.overlay_input_shield():
            self.move_ai_cursor(gx1, gy1, label="Drag start", glide_ms=glide_ms, move_physical=False)
            self.emit({"type": "cursor_move", "x": gx2, "y": gy2, "label": "Dragging..."})
            self.emit({"type": "cursor_action", "action": "drag", "x": gx2, "y": gy2})
            self.ai_cursor_x, self.ai_cursor_y = gx2, gy2
            self.last_click_global = (gx2, gy2)

            with self._input_txn_lock:
                orig_x, orig_y = self.get_cursor_pos()
                self._mark_did_input()
                if restore_cursor:
                    with self.physical_cursor_shield(restore_to=(orig_x, orig_y)):
                        wb.drag_pointer(gx1, gy1, gx2, gy2, btn)
                        time.sleep(0.08)
                else:
                    wb.drag_pointer(gx1, gy1, gx2, gy2, btn)
                    time.sleep(0.08)
        return {"from": [gx1, gy1], "to": [gx2, gy2], "button": btn}

    def scroll(
        self,
        gx: int,
        gy: int,
        dy: int,
        dx: int = 0,
        restore_cursor: bool = True,
        glide_ms: int = 180,
    ) -> dict[str, Any]:
        with self.overlay_input_shield():
            self.move_ai_cursor(gx, gy, label="Scroll", glide_ms=glide_ms, move_physical=False)
            self.emit({"type": "cursor_action", "action": "scroll", "x": gx, "y": gy})
            with self._input_txn_lock:
                orig_x, orig_y = self.get_cursor_pos()
                self._mark_did_input()
                if restore_cursor:
                    with self.physical_cursor_shield(restore_to=(orig_x, orig_y)):
                        wb.scroll_pointer(gx, gy, dy, dx)
                        time.sleep(0.06)
                else:
                    wb.scroll_pointer(gx, gy, dy, dx)
                    time.sleep(0.06)
        return {"x": gx, "y": gy, "dx": dx, "dy": dy}

    def type_text(
        self,
        text: str,
        press_enter: bool = False,
        delay_ms: int = 5,
        direct_type: bool = False,
    ) -> dict[str, Any]:
        self.emit({"type": "cursor_action", "action": "type", "x": self.ai_cursor_x, "y": self.ai_cursor_y, "text": text[:40]})
        with self._input_txn_lock:
            self._mark_did_input()
            use_paste = (not direct_type) and ("\n" in text or len(text) > 28)
            if use_paste:
                ok_prev, prev_clip, _ = wb.clipboard_get()
                ok_cp, err_cp = wb.clipboard_set(text)
                if not ok_cp:
                    raise RuntimeError(f"Clipboard set failed: {err_cp}")
                time.sleep(0.04)
                wb.send_vk_hotkey("ctrl+v")
                time.sleep(0.07)
                if ok_prev and prev_clip:
                    wb.clipboard_set(prev_clip)
            else:
                wb.send_unicode_text(text, delay_ms=max(2, delay_ms))
                time.sleep(0.03)
            if press_enter:
                wb.send_vk_hotkey("Return")
                time.sleep(0.04)
        return {"typed_chars": len(text), "press_enter": press_enter}

    def press_hotkey(self, keys: str) -> dict[str, Any]:
        with self._input_txn_lock:
            self._mark_did_input()
            wb.send_vk_hotkey(keys)
            time.sleep(0.03)
        return {"hotkey": keys}


class CloneDesktopProxy(DesktopController):
    """Isolated sub-cursor view sharing physical SendInput/locks while maintaining its own visuals and OCR cache."""

    def __init__(self, parent: DesktopController, clone_id: str, start_x: int, start_y: int) -> None:
        self._parent = parent
        self.clone_id = clone_id
        self.ai_cursor_x: int = int(start_x)
        self.ai_cursor_y: int = int(start_y)
        self.last_shot: ScreenshotMeta | None = None
        self.last_elements: list[UiElement] = []
        self.last_elements_monitor: str = ""
        self.last_click_global: tuple[int, int] | None = None
        self._in_task: bool = True
        self._task_orig_cursor: tuple[int, int] | None = None
        self._task_did_input: bool = False
        self._shield_lock = parent._shield_lock
        self._shield_depth: int = 0
        self._input_shield_depth: int = 0
        self._os_cursor_hidden: bool = False
        self._input_txn_lock = parent._input_txn_lock
        self._capture_lock = parent._capture_lock

    def _mark_did_input(self) -> None:
        self._parent._mark_did_input()

    def begin_task(self) -> None:
        return None

    def end_task(self, restore_cursor: bool = True, aborted: bool = False) -> None:
        return None

    def emit(self, ev: dict[str, Any]) -> None:
        kind = ev.get("type", "")
        if kind in ("capture_hide", "input_shield"):
            self._parent.emit(ev)
            return
        mapped = dict(ev)
        if kind == "cursor_move":
            mapped["type"] = "clone_move"
            mapped["id"] = self.clone_id
        elif kind == "cursor_action":
            mapped["type"] = "clone_action"
            mapped["id"] = self.clone_id
        elif kind == "cursor_status":
            mapped["type"] = "clone_status"
            mapped["id"] = self.clone_id
        else:
            return
        self._parent.emit(mapped)
