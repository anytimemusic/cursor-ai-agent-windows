from __future__ import annotations

import ctypes
import math
import sys
import time
from ctypes import wintypes
from typing import Any

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from agent import win32_backend as wb
from agent.bridge import AgentBridge

IS_WINDOWS = sys.platform == "win32"

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000

HOTKEY_TOGGLE_ID = 1001
HOTKEY_STOP_ID = 1002
HOTKEY_PAUSE_ID = 1003


def _set_hwnd_clickthrough(widget: QWidget, clickthrough: bool, no_activate: bool = True) -> None:
    if not IS_WINDOWS:
        return
    try:
        hwnd = int(widget.winId())
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        ex = ctypes.c_ulong(user32.GetWindowLongW(hwnd, GWL_EXSTYLE)).value
        ex |= WS_EX_LAYERED | WS_EX_TOOLWINDOW
        if clickthrough:
            ex |= WS_EX_TRANSPARENT
        else:
            ex &= ~WS_EX_TRANSPARENT
        if no_activate:
            ex |= WS_EX_NOACTIVATE
        else:
            ex &= ~WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ctypes.c_long(ex & 0xFFFFFFFF))
    except Exception:
        pass


def _paint_pointer_arrow(painter: QPainter, x: float, y: float, fill_col: QColor, scale_factor: float = 1.0) -> None:
    painter.save()
    painter.translate(x, y)
    painter.scale(scale_factor, scale_factor)

    # Soft drop shadow
    painter.save()
    painter.translate(1.5, 2.0)
    shadow_path = QPainterPath()
    shadow_path.moveTo(0, 0)
    shadow_path.lineTo(3.2, 21.5)
    shadow_path.lineTo(8.2, 15.8)
    shadow_path.lineTo(14.0, 24.5)
    shadow_path.lineTo(16.8, 23.0)
    shadow_path.lineTo(11.2, 14.3)
    shadow_path.lineTo(19.2, 12.6)
    shadow_path.closeSubpath()
    painter.fillPath(shadow_path, QColor(0, 0, 0, 95))
    painter.restore()

    grad = QLinearGradient(0, 0, 18, 24)
    grad.setColorAt(0.0, fill_col.lighter(125))
    grad.setColorAt(1.0, fill_col)

    arrow = QPainterPath()
    arrow.moveTo(0, 0)
    arrow.lineTo(3.0, 21.0)
    arrow.lineTo(8.0, 15.2)
    arrow.lineTo(13.6, 23.8)
    arrow.lineTo(16.4, 22.3)
    arrow.lineTo(10.8, 13.7)
    arrow.lineTo(18.8, 12.0)
    arrow.closeSubpath()

    painter.fillPath(arrow, grad)
    pen = QPen(QColor("#070b14"), 1.6)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawPath(arrow)

    inner = QPainterPath()
    inner.moveTo(1.7, 2.5)
    inner.lineTo(3.9, 17.8)
    inner.lineTo(7.6, 13.6)
    inner.lineTo(15.2, 11.4)
    inner.closeSubpath()
    painter.setPen(QPen(QColor(255, 255, 255, 110), 0.9))
    painter.drawPath(inner)
    painter.restore()


def _make_tray_icon(accent: str = "#38bdf8") -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor("#090d16"))
    p.setPen(QPen(QColor(accent), 1.6))
    p.drawRoundedRect(1, 1, 30, 30, 8, 8)
    _paint_pointer_arrow(p, 8, 5, QColor(accent), 0.9)
    p.end()
    return QIcon(pm)


class EventSignalBus(QObject):
    event_received = Signal(dict)


class HotkeyWindow(QWidget):
    """Hidden top-level message window receiving Windows 11 global RegisterHotKey events."""

    def __init__(self, on_toggle, on_stop, on_pause) -> None:
        super().__init__()
        self._on_toggle = on_toggle
        self._on_stop = on_stop
        self._on_pause = on_pause
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.resize(1, 1)
        if IS_WINDOWS:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.RegisterHotKey(hwnd, HOTKEY_TOGGLE_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, 0x20)  # Ctrl+Alt+Space
            user32.RegisterHotKey(hwnd, HOTKEY_STOP_ID, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, ord("X"))  # Ctrl+Shift+X
            user32.RegisterHotKey(hwnd, HOTKEY_PAUSE_ID, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("P"))  # Ctrl+Alt+P

    def nativeEvent(self, eventType, message):
        if IS_WINDOWS and eventType == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                hk = int(msg.wParam)
                if hk == HOTKEY_TOGGLE_ID:
                    self._on_toggle()
                elif hk == HOTKEY_STOP_ID:
                    self._on_stop()
                elif hk == HOTKEY_PAUSE_ID:
                    self._on_pause()
                return True, 0
        return super().nativeEvent(eventType, message)

    def unregister(self) -> None:
        if IS_WINDOWS:
            try:
                hwnd = int(self.winId())
                user32 = ctypes.windll.user32
                for hk in (HOTKEY_TOGGLE_ID, HOTKEY_STOP_ID, HOTKEY_PAUSE_ID):
                    user32.UnregisterHotKey(hwnd, hk)
            except Exception:
                pass


class CursorCanvasWindow(QWidget):
    """Full-screen per-monitor transparent click-through layer rendering the AI cursor, trail, ripples & Swarm clones."""

    def __init__(self, screen, state: "OverlayCoordinator", mon_phys: dict[str, Any]) -> None:
        super().__init__()
        self._screen = screen
        self._state = state
        self._mon_phys = mon_phys

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        geo = screen.geometry()
        self.setGeometry(geo)
        self.show()
        _set_hwnd_clickthrough(self, clickthrough=True, no_activate=True)

    def update_monitor_rect(self, screen, mon_phys: dict[str, Any]) -> None:
        self._screen = screen
        self._mon_phys = mon_phys
        self.setGeometry(screen.geometry())
        _set_hwnd_clickthrough(self, clickthrough=True, no_activate=True)

    def phys_to_local(self, gx: float, gy: float) -> tuple[float, float]:
        dpr = max(0.5, float(self._screen.devicePixelRatio() or 1.0))
        px = float(self._mon_phys.get("x", 0))
        py = float(self._mon_phys.get("y", 0))
        return (gx - px) / dpr, (gy - py) / dpr

    def paintEvent(self, event) -> None:
        if self._state.capture_hidden or not self._state.cursor_visible:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor(self._state.accent_color)

        # 1. Comet trail
        if self._state.show_trail:
            for px, py, life in self._state.trail_points:
                if life <= 0.02:
                    continue
                lx, ly = self.phys_to_local(px, py)
                if lx < -40 or ly < -40 or lx > self.width() + 40 or ly > self.height() + 40:
                    continue
                sz = 2.5 + 5.8 * life
                c = QColor(accent)
                c.setAlphaF(0.34 * life * life)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(c)
                painter.drawEllipse(QPointF(lx + 2, ly + 2), sz / 2.0, sz / 2.0)

        # 2. Main AI Cursor (plus ripple ring)
        cx, cy = self.phys_to_local(self._state.render_x, self._state.render_y)
        if -120 <= cx <= self.width() + 120 and -120 <= cy <= self.height() + 120:
            if self._state.ripple_life > 0.01:
                prog = 1.0 - self._state.ripple_life
                rad = (10.0 + 36.0 * prog) / 2.0
                rc = QColor(self._state.ripple_color)
                rc.setAlphaF(0.85 * self._state.ripple_life)
                painter.setPen(QPen(rc, 2.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), rad, rad)

            painter.save()
            dy_idle = self._state.idle_float_y if (not self._state.in_flight and not self._state.dragging and self._state.status == "idle" and not self._state.expanded) else 0.0
            painter.translate(cx, cy + dy_idle)
            painter.rotate(self._state.velocity_tilt)
            sc = self._state.click_scale * self._state.velocity_stretch
            painter.scale(sc, sc)
            _paint_pointer_arrow(painter, 0.0, 0.0, accent, 1.0)
            painter.restore()

        # 3. Swarm Clones (Kage Bunshin)
        for clone in self._state.clones.values():
            if not clone["active"]:
                continue
            klx, kly = self.phys_to_local(clone["rx"], clone["ry"])
            if klx < -100 or kly < -100 or klx > self.width() + 100 or kly > self.height() + 100:
                continue
            ccol = QColor(clone["color"])
            if clone["ripple"] > 0.01:
                prog = 1.0 - clone["ripple"]
                rc = QColor(ccol)
                rc.setAlphaF(0.85 * clone["ripple"])
                painter.setPen(QPen(rc, 2.0))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(klx, kly), 4 + 16 * prog, 4 + 16 * prog)

            _paint_pointer_arrow(painter, klx, kly, ccol, 0.84)
            lbl = clone["label"]
            if lbl:
                font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
                painter.setFont(font)
                fm = painter.fontMetrics()
                bw = max(56, fm.horizontalAdvance(lbl) + 16)
                rect = QRectF(klx + 15, kly + 16, bw, 22)
                painter.setBrush(QColor(11, 16, 29, 235))
                painter.setPen(QPen(ccol, 1.1))
                painter.drawRoundedRect(rect, 11, 11)
                painter.setPen(QColor("#f8fafc"))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, lbl)


class CursorPillWindow(QWidget):
    """Unified morphing pill + Prompt HUD + Settings dialog anchored 1:1 right beside the AI cursor tip."""

    def __init__(self, state: "OverlayCoordinator") -> None:
        super().__init__()
        self._state = state
        self._dragging = False
        self._drag_press_global = QPointF()
        self._drag_start_cursor = (960.0, 540.0)
        self._hover_collapse_timer = QTimer(self)
        self._hover_collapse_timer.setSingleShot(True)
        self._hover_collapse_timer.timeout.connect(self._on_hover_leave_timeout)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._build_ui()
        self.sync_mode()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.card = QWidget(self)
        self.card.setObjectName("pillCard")
        root.addWidget(self.card)

        self.card_layout = QVBoxLayout(self.card)
        self.card_layout.setContentsMargins(10, 4, 10, 4)
        self.card_layout.setSpacing(6)

        # ---- Collapsed view ----
        self.collapsed_widget = QWidget(self.card)
        c_row = QHBoxLayout(self.collapsed_widget)
        c_row.setContentsMargins(0, 0, 0, 0)
        c_row.setSpacing(6)

        self.lbl_status = QLabel("Type here...", self.collapsed_widget)
        self.lbl_status.setStyleSheet("color: #f8fafc; font-family: 'Segoe UI'; font-size: 11px; font-weight: 600;")
        c_row.addWidget(self.lbl_status, 1)

        self.btn_mini_pause = QPushButton("⏸", self.collapsed_widget)
        self.btn_mini_pause.setFixedSize(18, 18)
        self.btn_mini_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mini_pause.clicked.connect(lambda: self._state.bridge.handle_command({"cmd": "toggle_pause"}))
        c_row.addWidget(self.btn_mini_pause)

        self.btn_mini_stop = QPushButton("■", self.collapsed_widget)
        self.btn_mini_stop.setFixedSize(18, 18)
        self.btn_mini_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mini_stop.clicked.connect(lambda: self._state.bridge.handle_command({"cmd": "stop"}))
        c_row.addWidget(self.btn_mini_stop)

        self.btn_mini_close = QPushButton("×", self.collapsed_widget)
        self.btn_mini_close.setFixedSize(18, 18)
        self.btn_mini_close.setToolTip("Sembunyikan kursor AI (Ctrl+Alt+Space untuk munculkan lagi)")
        self.btn_mini_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_mini_close.clicked.connect(self._state.dismiss_cursor)
        c_row.addWidget(self.btn_mini_close)

        self.card_layout.addWidget(self.collapsed_widget)

        # ---- Expanded Prompt HUD ----
        self.expanded_widget = QWidget(self.card)
        e_col = QVBoxLayout(self.expanded_widget)
        e_col.setContentsMargins(2, 4, 2, 4)
        e_col.setSpacing(6)

        self.lbl_err = QLabel("", self.expanded_widget)
        self.lbl_err.setWordWrap(True)
        self.lbl_err.setStyleSheet("color: #f8fafc; background: rgba(30,41,59,180); border: 1px solid #334155; border-radius: 7px; padding: 5px 7px; font-size: 11px;")
        self.lbl_err.hide()
        e_col.addWidget(self.lbl_err)

        self.prompt_input = QLineEdit(self.expanded_widget)
        self.prompt_input.setPlaceholderText("Type here...")
        self.prompt_input.setFixedHeight(32)
        self.prompt_input.returnPressed.connect(self._submit_prompt)
        e_col.addWidget(self.prompt_input)

        act_row = QHBoxLayout()
        act_row.setSpacing(5)
        self.btn_demo = QPushButton("Demo", self.expanded_widget)
        self.btn_demo.setFixedHeight(24)
        self.btn_demo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_demo.clicked.connect(lambda: self._state.bridge.handle_command({"cmd": "demo"}))
        act_row.addWidget(self.btn_demo)

        self.btn_settings = QPushButton("⚙", self.expanded_widget)
        self.btn_settings.setFixedSize(26, 24)
        self.btn_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_settings.clicked.connect(self._open_settings)
        act_row.addWidget(self.btn_settings)

        self.btn_exp_pause = QPushButton("⏸ Pause", self.expanded_widget)
        self.btn_exp_pause.setFixedHeight(24)
        self.btn_exp_pause.clicked.connect(lambda: self._state.bridge.handle_command({"cmd": "toggle_pause"}))
        act_row.addWidget(self.btn_exp_pause)

        self.btn_exp_stop = QPushButton("■ Stop", self.expanded_widget)
        self.btn_exp_stop.setFixedHeight(24)
        self.btn_exp_stop.clicked.connect(lambda: self._state.bridge.handle_command({"cmd": "stop"}))
        act_row.addWidget(self.btn_exp_stop)

        act_row.addStretch(1)

        self.btn_run = QPushButton("Run ▶", self.expanded_widget)
        self.btn_run.setFixedHeight(24)
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_run.clicked.connect(self._submit_prompt)
        act_row.addWidget(self.btn_run)

        self.btn_exp_close = QPushButton("×", self.expanded_widget)
        self.btn_exp_close.setFixedSize(24, 24)
        self.btn_exp_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_exp_close.clicked.connect(self._state.dismiss_cursor)
        act_row.addWidget(self.btn_exp_close)

        e_col.addLayout(act_row)
        self.card_layout.addWidget(self.expanded_widget)

        # ---- Inline Settings Panel ----
        self.settings_widget = QWidget(self.card)
        s_outer = QVBoxLayout(self.settings_widget)
        s_outer.setContentsMargins(2, 4, 2, 4)
        s_outer.setSpacing(6)

        hdr = QHBoxLayout()
        lbl_t = QLabel("Settings", self.settings_widget)
        lbl_t.setStyleSheet("color: #f8fafc; font-weight: 700; font-size: 12px;")
        hdr.addWidget(lbl_t, 1)
        btn_back = QPushButton("←", self.settings_widget)
        btn_back.setFixedSize(24, 24)
        btn_back.setToolTip("Kembali tanpa menyimpan")
        btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_back.clicked.connect(self._close_settings)
        hdr.addWidget(btn_back)
        s_outer.addLayout(hdr)

        scroll = QScrollArea(self.settings_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; } QWidget { background: transparent; }")
        form_wrap = QWidget()
        form = QVBoxLayout(form_wrap)
        form.setContentsMargins(0, 0, 4, 0)
        form.setSpacing(5)

        self._draft_keys: dict[str, str] = {}
        self._current_provider_edit = "9router"
        self._show_api_key = False

        form.addWidget(self._small_lbl("Provider"))
        self.cmb_provider = QComboBox(form_wrap)
        self.cmb_provider.addItems(["9router", "openrouter", "openai", "ollama", "gemini"])
        self.cmb_provider.currentTextChanged.connect(self._on_provider_changed)
        form.addWidget(self.cmb_provider)

        form.addWidget(self._small_lbl("Base URL"))
        self.inp_base_url = QLineEdit(form_wrap)
        form.addWidget(self.inp_base_url)

        form.addWidget(self._small_lbl("API Key"))
        key_row = QHBoxLayout()
        key_row.setSpacing(4)
        self.inp_api_key = QLineEdit(form_wrap)
        self.inp_api_key.setPlaceholderText("Masukkan API Key...")
        self.inp_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.inp_api_key, 1)
        self.btn_toggle_key = QPushButton("Lihat", form_wrap)
        self.btn_toggle_key.setFixedHeight(24)
        self.btn_toggle_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_key.clicked.connect(self._toggle_api_key_visibility)
        key_row.addWidget(self.btn_toggle_key)
        form.addLayout(key_row)

        m_row = QHBoxLayout()
        m_row.addWidget(self._small_lbl("Model (kosongkan = auto)"), 1)
        btn_ref = QPushButton("Cek Model", form_wrap)
        btn_ref.setFixedHeight(21)
        btn_ref.setToolTip("Cek koneksi & muat daftar model")
        btn_ref.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ref.clicked.connect(self._trigger_model_check)
        m_row.addWidget(btn_ref)
        form.addLayout(m_row)

        self.cmb_model = QComboBox(form_wrap)
        self.cmb_model.setEditable(True)
        if self.cmb_model.lineEdit() is not None:
            self.cmb_model.lineEdit().setPlaceholderText("Auto (atau ketik / pilih model)")
        form.addWidget(self.cmb_model)

        self.lbl_models_info = QLabel("Cek model...", form_wrap)
        self.lbl_models_info.setWordWrap(True)
        self.lbl_models_info.setStyleSheet("color: #94a3b8; font-size: 10px;")
        form.addWidget(self.lbl_models_info)

        form.addWidget(self._small_lbl("Warna Kursor AI"))
        sw_row = QHBoxLayout()
        sw_row.setSpacing(6)
        self._selected_accent = "#38bdf8"
        self._swatch_btns: dict[str, QPushButton] = {}
        for col_hex in ("#38bdf8", "#34d399", "#a78bfa", "#f59e0b", "#fb7185", "#e2e8f0"):
            sb = QPushButton("", form_wrap)
            sb.setFixedSize(18, 18)
            sb.clicked.connect(lambda _=False, ch=col_hex: self._pick_accent(ch))
            sw_row.addWidget(sb)
            self._swatch_btns[col_hex] = sb
        sw_row.addStretch(1)
        form.addLayout(sw_row)

        self._restore_val = True
        self.btn_tog_restore = QPushButton("Kembalikan mouse asli: ON", form_wrap)
        self.btn_tog_restore.clicked.connect(self._toggle_restore)
        form.addWidget(self.btn_tog_restore)

        self._trail_val = True
        self.btn_tog_trail = QPushButton("Jejak kursor (trail): ON", form_wrap)
        self.btn_tog_trail.clicked.connect(self._toggle_trail)
        form.addWidget(self.btn_tog_trail)

        scroll.setWidget(form_wrap)
        s_outer.addWidget(scroll, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 2, 0, 0)
        footer.setSpacing(6)
        btn_cancel = QPushButton("Batal", self.settings_widget)
        btn_cancel.setFixedHeight(28)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.clicked.connect(self._close_settings)
        footer.addWidget(btn_cancel, 1)

        btn_save = QPushButton("Simpan & Apply", self.settings_widget)
        btn_save.setFixedHeight(28)
        btn_save.setObjectName("btnSave")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._save_settings)
        footer.addWidget(btn_save, 2)
        s_outer.addLayout(footer)

        self.card_layout.addWidget(self.settings_widget)

        self.apply_styles()

    def _small_lbl(self, txt: str) -> QLabel:
        lb = QLabel(txt)
        lb.setStyleSheet("color: #94a3b8; font-size: 10px;")
        return lb

    def apply_styles(self) -> None:
        accent = self._state.accent_color
        expanded = self._state.expanded
        is_busy = self._state.is_busy
        bcol = accent if (expanded or is_busy) else "#475569"
        bw = "1.4px" if expanded else "1.1px"
        radius = "14px" if expanded else "13px"
        bg = "rgba(11, 16, 29, 245)" if expanded else "rgba(11, 16, 29, 232)"
        self.card.setStyleSheet(
            f"#pillCard {{ background: {bg}; border: {bw} solid {bcol}; border-radius: {radius}; }}"
            "QLineEdit { background: #111827; color: #f8fafc; border: 1px solid #334155; border-radius: 7px; padding: 3px 8px; font-size: 11px; }"
            f"QLineEdit:focus {{ border: 1.2px solid {accent}; }}"
            "QComboBox { background: #111827; color: #f8fafc; border: 1px solid #334155; border-radius: 7px; padding: 3px 6px; font-size: 11px; }"
            "QComboBox QAbstractItemView { background: #0f172a; color: #f8fafc; selection-background-color: #1e293b; }"
            "QPushButton { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 6px; padding: 2px 7px; font-size: 10px; font-weight: 600; }"
            "QPushButton:hover { background: #334155; }"
            f"#btnSave {{ background: {accent}; color: #090d16; border: none; font-weight: 700; font-size: 11px; }}"
        )
        self.btn_mini_close.setStyleSheet("QPushButton { background: #1e293b; color: #94a3b8; border: none; border-radius: 9px; font-weight: 700; } QPushButton:hover { background: #7f1d1d; color: #fecaca; }")
        self.btn_mini_pause.setStyleSheet("QPushButton { background: #1e293b; color: #e2e8f0; border: none; border-radius: 9px; }")
        self.btn_mini_stop.setStyleSheet("QPushButton { background: #334155; color: #f8fafc; border: none; border-radius: 9px; }")
        for col_hex, sb in self._swatch_btns.items():
            b = "2px solid #ffffff" if self._selected_accent == col_hex else "none"
            sb.setStyleSheet(f"QPushButton {{ background: {col_hex}; border: {b}; border-radius: 9px; padding: 0; }}")

    def _submit_prompt(self) -> None:
        txt = self.prompt_input.text().strip()
        if not txt:
            return
        self.prompt_input.clear()
        self._state.send_prompt(txt)

    def _toggle_api_key_visibility(self) -> None:
        self._show_api_key = not self._show_api_key
        self.inp_api_key.setEchoMode(QLineEdit.EchoMode.Normal if self._show_api_key else QLineEdit.EchoMode.Password)
        self.btn_toggle_key.setText("Tutup" if self._show_api_key else "Lihat")

    def _trigger_model_check(self) -> None:
        self.lbl_models_info.setText("Mengecek koneksi & daftar model...")
        self.lbl_models_info.setStyleSheet("color: #94a3b8; font-size: 10px;")
        self._state.bridge.handle_command({
            "cmd": "check_models",
            "base_url": self.inp_base_url.text().strip(),
            "api_key": self.inp_api_key.text().strip(),
        })

    def _on_provider_changed(self, prov: str) -> None:
        old_prov = self._current_provider_edit
        if old_prov:
            self._draft_keys[old_prov] = self.inp_api_key.text()
        self._current_provider_edit = prov

        presets = self._state.presets or {}
        p = presets.get(prov)
        dk = str(p.get("default_key") or "") if p else ""
        if p:
            self.inp_base_url.setText(str(p.get("base_url") or ""))

        if prov in self._draft_keys:
            new_key = self._draft_keys[prov]
        else:
            cur_key = self.inp_api_key.text().strip()
            new_key = dk if (dk or cur_key in ("", "9router", "ollama")) else ""
        self.inp_api_key.setText(new_key)

        saved_prov = str(self._state.config.get("provider") or "")
        saved_model = str(self._state.config.get("model") or "") if prov == saved_prov else ""
        self.cmb_model.blockSignals(True)
        self.cmb_model.clear()
        self.cmb_model.addItem("")
        self.cmb_model.setCurrentText(saved_model)
        self.cmb_model.blockSignals(False)

        if new_key.strip() or prov in ("9router", "ollama"):
            self._trigger_model_check()
        else:
            self.lbl_models_info.setText("Isi API Key lalu klik 'Cek Model' atau langsung 'Simpan & Apply'.")
            self.lbl_models_info.setStyleSheet("color: #94a3b8; font-size: 10px;")

    def _pick_accent(self, col_hex: str) -> None:
        self._selected_accent = col_hex
        self.apply_styles()

    def _toggle_restore(self) -> None:
        self._restore_val = not self._restore_val
        self.btn_tog_restore.setText(f"Kembalikan mouse asli: {'ON' if self._restore_val else 'OFF'}")

    def _toggle_trail(self) -> None:
        self._trail_val = not self._trail_val
        self.btn_tog_trail.setText(f"Jejak kursor (trail): {'ON' if self._trail_val else 'OFF'}")

    def _open_settings(self) -> None:
        cfg = self._state.config
        prov = str(cfg.get("provider") or "9router")
        cur_key = str(cfg.get("api_key") or "")
        self._current_provider_edit = prov
        self._draft_keys = {prov: cur_key}
        self._show_api_key = False
        self.inp_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.btn_toggle_key.setText("Lihat")

        self.cmb_provider.blockSignals(True)
        idx = self.cmb_provider.findText(prov)
        if idx >= 0:
            self.cmb_provider.setCurrentIndex(idx)
        self.cmb_provider.blockSignals(False)

        self.inp_base_url.setText(str(cfg.get("base_url") or ""))
        self.inp_api_key.setText(cur_key)
        self.cmb_model.setCurrentText(str(cfg.get("model") or ""))
        self._selected_accent = str(cfg.get("cursor_accent") or "#38bdf8")
        self._restore_val = bool(cfg.get("restore_cursor", True))
        self._trail_val = bool(cfg.get("show_trail", True))
        self.btn_tog_restore.setText(f"Kembalikan mouse asli: {'ON' if self._restore_val else 'OFF'}")
        self.btn_tog_trail.setText(f"Jejak kursor (trail): {'ON' if self._trail_val else 'OFF'}")
        self._state.settings_tab = True
        self._state.wants_keyboard = True
        self.sync_mode()
        self._state._reposition_pill()
        self.activateWindow()
        if not cur_key.strip():
            self.inp_api_key.setFocus()

    def _close_settings(self) -> None:
        self._state.settings_tab = False
        self.sync_mode()
        self._state._reposition_pill()

    def _save_settings(self) -> None:
        self._state.last_error = ""
        self._state.bridge.handle_command({
            "cmd": "save_config",
            "config": {
                "provider": self.cmb_provider.currentText().strip(),
                "base_url": self.inp_base_url.text().strip(),
                "api_key": self.inp_api_key.text().strip(),
                "model": self.cmb_model.currentText().strip(),
                "cursor_accent": self._selected_accent,
                "restore_cursor": self._restore_val,
                "show_trail": self._trail_val,
            },
        })
        self._state.settings_tab = False
        self.sync_mode()
        self._state._reposition_pill()

    def update_models_status(self, ev: dict[str, Any]) -> None:
        if ev.get("ok"):
            models = list(ev.get("models") or [])
            cur = self.cmb_model.currentText()
            self.cmb_model.blockSignals(True)
            self.cmb_model.clear()
            self.cmb_model.addItem("")
            for m in models[:40]:
                self.cmb_model.addItem(str(m))
            self.cmb_model.setCurrentText(cur)
            self.cmb_model.blockSignals(False)
            self.lbl_models_info.setText(f"{len(models)} model aktif di {ev.get('base_url', '')}")
            self.lbl_models_info.setStyleSheet("color: #34d399; font-size: 10px;")
        else:
            self.lbl_models_info.setText(f"Offline: {ev.get('error', '')}")
            self.lbl_models_info.setStyleSheet("color: #f87171; font-size: 10px;")

    def sync_mode(self) -> None:
        exp = self._state.expanded
        st = self._state.settings_tab
        busy = self._state.is_busy

        self.collapsed_widget.setVisible(not exp)
        self.expanded_widget.setVisible(exp and not st)
        self.settings_widget.setVisible(exp and st)

        self.btn_mini_pause.setVisible(busy)
        self.btn_mini_pause.setText("▶" if self._state.status == "paused" else "⏸")
        self.btn_mini_stop.setVisible(busy)
        self.btn_mini_close.setVisible(not busy)

        self.btn_exp_pause.setVisible(busy)
        self.btn_exp_pause.setText("▶ Lanjut" if self._state.status == "paused" else "⏸ Pause")
        self.btn_exp_stop.setVisible(busy)
        self.btn_run.setEnabled(not busy and bool(self.prompt_input.text().strip() or True))

        if self._state.last_error:
            self.lbl_err.setText(self._state.last_error)
            self.lbl_err.show()
        else:
            self.lbl_err.hide()

        self.lbl_status.setText(self._state.cursor_label or "Type here...")

        if st:
            cw = self._state._find_canvas_for_phys(self._state.render_x, self._state.render_y)
            max_h = max(320, cw._screen.geometry().height() - 80) if cw else 430
            self.setFixedSize(364, min(430, max_h))
        elif exp:
            h = 122 if self._state.last_error else 82
            self.setFixedSize(304, h)
        else:
            fm = self.lbl_status.fontMetrics()
            tw = fm.horizontalAdvance(self.lbl_status.text())
            extra = 48 if busy else 26
            self.setFixedSize(max(102, min(260, tw + extra + 22)), 28)

        self.apply_styles()

        should_show = (not self._state.capture_hidden) and self._state.cursor_visible
        if should_show:
            if not self.isVisible():
                self.show()
            _set_hwnd_clickthrough(
                self,
                clickthrough=self._state.input_shield,
                no_activate=not (exp and self._state.wants_keyboard),
            )
            if exp and (not st) and self._state.wants_keyboard:
                self.activateWindow()
                self.prompt_input.setFocus()
        else:
            if self.isVisible():
                self.hide()

    def enterEvent(self, event) -> None:
        self._hover_collapse_timer.stop()
        if not self._state.cursor_dismissed and not self._state.is_busy and not self._dragging and not self._state.input_shield:
            if not self._state.settings_tab:
                self._state.hover_open = True
                self._state.wants_keyboard = True
                self.sync_mode()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._state.settings_tab and not self._dragging and not self._state.hud_open:
            self._hover_collapse_timer.start(140)
        super().leaveEvent(event)

    def _on_hover_leave_timeout(self) -> None:
        if not self.underMouse() and not self._state.settings_tab and not self._dragging and not self._state.hud_open:
            self._state.collapse_all()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._state.status == "idle":
            self._dragging = True
            self._drag_press_global = event.globalPosition()
            self._drag_start_cursor = (self._state.target_x, self._state.target_y)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            delta = event.globalPosition() - self._drag_press_global
            # Map Qt logical drag delta via screen DPR back into physical pixels
            scr = QApplication.screenAt(event.globalPosition().toPoint()) or QApplication.primaryScreen()
            dpr = max(0.5, float(scr.devicePixelRatio() or 1.0)) if scr else 1.0
            nx = self._drag_start_cursor[0] + delta.x() * dpr
            ny = self._drag_start_cursor[1] + delta.y() * dpr
            vx, vy, vw, vh = self._state.virtual_bounds
            nx = max(vx + 12, min(vx + vw - 36, nx))
            ny = max(vy + 12, min(vy + vh - 36, ny))
            self._state.dragging = True
            self._state.cursor_label = "Geser..."
            self._state.snap_cursor(nx, ny)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self._state.dragging = False
            self._state.bridge.handle_command({
                "cmd": "move_cursor",
                "x": round(self._state.target_x),
                "y": round(self._state.target_y),
                "silent": True,
            })
            if self._state.status == "idle":
                self._state.cursor_label = "Type here..."
            self.sync_mode()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self._state.collapse_all()
            return
        super().keyPressEvent(event)


class OverlayCoordinator(QObject):
    """Coordinates spring physics, multi-monitor Qt canvas windows, floating pill HUD, tray & hotkeys."""

    def __init__(self, app: QApplication, bridge: AgentBridge) -> None:
        super().__init__()
        self.app = app
        self.bridge = bridge
        self.config: dict[str, Any] = bridge.get_config()
        self.presets: dict[str, Any] = {}

        mons = wb.get_monitors()
        self.monitors = mons
        self.virtual_bounds = self._calc_virtual_bounds(mons)
        focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
        ix = float(int(focused["x"]) + int(focused["logical_w"]) // 2) if focused else 960.0
        iy = float(int(focused["y"]) + int(focused["logical_h"]) // 2) if focused else 540.0

        self.target_x: float = ix
        self.target_y: float = iy
        self.render_x: float = ix
        self.render_y: float = iy
        self.vel_x: float = 0.0
        self.vel_y: float = 0.0
        self.velocity_tilt: float = 0.0
        self.velocity_stretch: float = 1.0
        self.click_scale: float = 1.0
        self.idle_float_y: float = 0.0
        self._idle_phase: float = 0.0
        self.in_flight: bool = False
        self.dragging: bool = False

        self.ripple_life: float = 0.0
        self.ripple_color: str = "#38bdf8"
        self.trail_points: list[list[float]] = []

        self.clones: dict[str, dict[str, Any]] = {}

        self.cursor_label: str = "Type here..."
        self.status: str = "idle"
        self.last_error: str = ""
        self.capture_hidden: bool = False
        self.input_shield: bool = False
        self.cursor_dismissed: bool = False
        self.hud_open: bool = False
        self.hover_open: bool = False
        self.settings_tab: bool = False
        self.wants_keyboard: bool = False

        self.bus = EventSignalBus()
        self.bus.event_received.connect(self._on_event, Qt.ConnectionType.QueuedConnection)
        bridge.add_listener(self.bus.event_received.emit)

        self.canvases: list[CursorCanvasWindow] = []
        self._rebuild_canvases()

        self.pill = CursorPillWindow(self)
        self._reposition_pill()

        self.hotkey_win = HotkeyWindow(
            on_toggle=lambda: self.bridge.handle_command({"cmd": "ui", "action": "toggle"}),
            on_stop=lambda: self.bridge.handle_command({"cmd": "stop"}),
            on_pause=lambda: self.bridge.handle_command({"cmd": "toggle_pause"}),
        )

        self.tray = QSystemTrayIcon(_make_tray_icon(self.accent_color), app)
        self._init_tray()

        self.label_reset_timer = QTimer(self)
        self.label_reset_timer.setSingleShot(True)
        self.label_reset_timer.timeout.connect(self._reset_idle_label)

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(16)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()

    @property
    def accent_color(self) -> str:
        return str(self.config.get("cursor_accent") or "#38bdf8")

    @property
    def show_trail(self) -> bool:
        return bool(self.config.get("show_trail", True))

    @property
    def is_busy(self) -> bool:
        return self.status in ("running", "paused")

    @property
    def expanded(self) -> bool:
        return self.hud_open or self.hover_open or self.settings_tab

    @property
    def cursor_visible(self) -> bool:
        return (not self.cursor_dismissed) and (bool(self.config.get("always_show_cursor", True)) or self.status != "idle" or self.expanded)

    def _calc_virtual_bounds(self, mons: list[dict[str, Any]]) -> tuple[int, int, int, int]:
        if not mons:
            return 0, 0, 1920, 1080
        x1 = min(int(m["x"]) for m in mons)
        y1 = min(int(m["y"]) for m in mons)
        x2 = max(int(m["x"]) + int(m["logical_w"]) for m in mons)
        y2 = max(int(m["y"]) + int(m["logical_h"]) for m in mons)
        return x1, y1, max(1, x2 - x1), max(1, y2 - y1)

    def _rebuild_canvases(self) -> None:
        screens = self.app.screens()
        mons = wb.get_monitors()
        self.monitors = mons
        self.virtual_bounds = self._calc_virtual_bounds(mons)
        for cw in self.canvases:
            cw.close()
        self.canvases.clear()

        for idx, scr in enumerate(screens):
            scr_name = str(scr.name() or "").replace("\\\\.\\", "").lower()
            mon = next((m for m in mons if str(m.get("name", "")).lower() == scr_name), None)
            if mon is None:
                mon = mons[idx] if idx < len(mons) else {"x": scr.geometry().x(), "y": scr.geometry().y(), "logical_w": scr.geometry().width(), "logical_h": scr.geometry().height()}
            self.canvases.append(CursorCanvasWindow(scr, self, mon))

    def _init_tray(self) -> None:
        menu = QMenu()
        a_toggle = QAction("Toggle / Panggil Kursor (Ctrl+Alt+Space)", menu)
        a_toggle.triggered.connect(lambda: self.bridge.handle_command({"cmd": "ui", "action": "toggle"}))
        a_demo = QAction("Demo Gerak Kursor", menu)
        a_demo.triggered.connect(lambda: self.bridge.handle_command({"cmd": "demo"}))
        a_pause = QAction("Pause / Lanjut (Ctrl+Alt+P)", menu)
        a_pause.triggered.connect(lambda: self.bridge.handle_command({"cmd": "toggle_pause"}))
        a_stop = QAction("Emergency STOP (Ctrl+Shift+X)", menu)
        a_stop.triggered.connect(lambda: self.bridge.handle_command({"cmd": "stop"}))
        a_quit = QAction("Keluar", menu)
        a_quit.triggered.connect(self.quit_app)
        menu.addAction(a_toggle)
        menu.addAction(a_demo)
        menu.addSeparator()
        menu.addAction(a_pause)
        menu.addAction(a_stop)
        menu.addSeparator()
        menu.addAction(a_quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Kursor AI Agent (Ctrl+Alt+Space)")
        self.tray.activated.connect(lambda reason: self.bridge.handle_command({"cmd": "ui", "action": "toggle"}) if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _reset_idle_label(self) -> None:
        if self.status == "idle" and not self.dragging:
            self.cursor_label = "Type here..."
            self.pill.sync_mode()

    def dismiss_cursor(self) -> None:
        self.hud_open = False
        self.hover_open = False
        self.settings_tab = False
        self.wants_keyboard = False
        self.cursor_dismissed = True
        self.pill.sync_mode()
        self._repaint_canvases()

    def collapse_all(self) -> None:
        self.hud_open = False
        self.hover_open = False
        self.settings_tab = False
        self.wants_keyboard = False
        self.pill.sync_mode()
        self._reposition_pill()

    def open_hud(self) -> None:
        self.cursor_dismissed = False
        self.hud_open = True
        self.wants_keyboard = True
        self.pill.sync_mode()
        self._reposition_pill()
        self._repaint_canvases()

    def send_prompt(self, prompt: str) -> None:
        self.cursor_dismissed = False
        self.last_error = ""
        self.hud_open = False
        self.hover_open = False
        self.settings_tab = False
        self.wants_keyboard = False
        self.pill.sync_mode()
        self._reposition_pill()
        self.bridge.handle_command({"cmd": "ask", "prompt": prompt, "attach_screen": True})

    def snap_cursor(self, gx: float, gy: float) -> None:
        self.target_x = gx
        self.target_y = gy
        self.render_x = gx
        self.render_y = gy
        self.vel_x = 0.0
        self.vel_y = 0.0
        self._reposition_pill()
        self._repaint_canvases()

    def _find_canvas_for_phys(self, gx: float, gy: float) -> CursorCanvasWindow | None:
        for cw in self.canvases:
            mp = cw._mon_phys
            if int(mp["x"]) <= gx < int(mp["x"]) + int(mp["logical_w"]) and int(mp["y"]) <= gy < int(mp["y"]) + int(mp["logical_h"]):
                return cw
        return self.canvases[0] if self.canvases else None

    def _reposition_pill(self) -> None:
        cw = self._find_canvas_for_phys(self.render_x, self.render_y)
        if cw is None:
            return
        geo = cw._screen.geometry()
        lx, ly = cw.phys_to_local(self.render_x, self.render_y)
        pw = self.pill.width()
        ph = self.pill.height()

        flip_left = lx > (geo.width() - max(200, pw + 28))
        flip_up = ly > (geo.height() - max(140, ph + 64))
        max_x = max(12.0, geo.width() - pw - 12.0)
        max_y = max(12.0, geo.height() - ph - 52.0)
        px_local = max(12.0, min(max_x, (lx - pw - 14.0) if flip_left else (lx + 18.0)))
        py_local = max(12.0, min(max_y, (ly - ph - 14.0) if flip_up else (ly + 18.0)))
        self.pill.move(round(geo.x() + px_local), round(geo.y() + py_local))

    def _repaint_canvases(self) -> None:
        for cw in self.canvases:
            if not self.capture_hidden and self.cursor_visible:
                if not cw.isVisible():
                    cw.show()
                    _set_hwnd_clickthrough(cw, clickthrough=True, no_activate=True)
                cw.update()
            else:
                if cw.isVisible():
                    cw.hide()

    def _on_event(self, ev: dict[str, Any]) -> None:
        t = str(ev.get("type") or "")
        if t == "init":
            if ev.get("config"):
                self.config = dict(ev["config"])
            if ev.get("presets"):
                self.presets = dict(ev["presets"])
            if ev.get("cursor"):
                self.snap_cursor(float(ev["cursor"]["x"]), float(ev["cursor"]["y"]))
            self.tray.setIcon(_make_tray_icon(self.accent_color))
            self.pill.sync_mode()
            self._reposition_pill()
        elif t == "config_updated":
            if ev.get("config"):
                self.config = dict(ev["config"])
                self.tray.setIcon(_make_tray_icon(self.accent_color))
                self.pill.sync_mode()
                self._reposition_pill()
        elif t == "models_status":
            self.pill.update_models_status(ev)
        elif t == "model_auto_selected":
            self.config["model"] = ev.get("model", "")
            self.pill.cmb_model.setCurrentText(str(ev.get("model") or ""))
        elif t == "cursor_move":
            self.target_x = float(ev.get("x", self.target_x))
            self.target_y = float(ev.get("y", self.target_y))
            if ev.get("label"):
                self.cursor_label = str(ev["label"])
                self.pill.sync_mode()
        elif t == "cursor_action":
            act = ev.get("action")
            btn = ev.get("button")
            self.ripple_color = "#f59e0b" if btn == "right" else ("#a78bfa" if act == "scroll" else self.accent_color)
            self.ripple_life = 1.0
            self.click_scale = 0.78
        elif t == "cursor_status":
            if ev.get("label"):
                self.cursor_label = str(ev["label"])
                self.pill.sync_mode()
        elif t == "capture_hide":
            self.capture_hidden = bool(ev.get("hidden"))
            self.pill.sync_mode()
            self._repaint_canvases()
        elif t == "input_shield":
            self.input_shield = bool(ev.get("active"))
            _set_hwnd_clickthrough(
                self.pill,
                clickthrough=self.input_shield,
                no_activate=not (self.expanded and self.wants_keyboard),
            )
        elif t == "agent_status":
            self.status = str(ev.get("status") or "idle")
            if self.status != "idle":
                self.cursor_dismissed = False
            if ev.get("label"):
                self.cursor_label = str(ev["label"])
            if self.status == "idle":
                self.input_shield = False
                self.label_reset_timer.start(3500)
            else:
                self.label_reset_timer.stop()
            self.pill.sync_mode()
            self._repaint_canvases()
        elif t == "error":
            self.last_error = str(ev.get("text") or "")
            self.pill.sync_mode()
        elif t == "clone_spawn":
            for c in ev.get("clones") or []:
                cid = str(c.get("id") or "")
                if not cid:
                    continue
                self.clones[cid] = {
                    "active": True,
                    "rx": float(c.get("x", self.render_x)),
                    "ry": float(c.get("y", self.render_y)),
                    "tx": float(c.get("target_x", self.render_x)),
                    "ty": float(c.get("target_y", self.render_y)),
                    "color": str(c.get("color") or "#34d399"),
                    "label": str(c.get("label") or ""),
                    "ripple": 0.0,
                }
            self._repaint_canvases()
        elif t == "clone_move":
            cid = str(ev.get("id") or "")
            c = self.clones.get(cid)
            if c:
                c["tx"] = float(ev.get("x", c["tx"]))
                c["ty"] = float(ev.get("y", c["ty"]))
                if ev.get("label"):
                    c["label"] = str(ev["label"])
        elif t == "clone_action":
            cid = str(ev.get("id") or "")
            c = self.clones.get(cid)
            if c:
                c["ripple"] = 1.0
        elif t == "clone_status":
            cid = str(ev.get("id") or "")
            c = self.clones.get(cid)
            if c and ev.get("label"):
                c["label"] = str(ev["label"])
        elif t == "clone_merge":
            self.clones.clear()
            self._repaint_canvases()
        elif t == "ui_command":
            act = str(ev.get("action") or "toggle")
            if act == "toggle":
                if self.cursor_dismissed or not self.hud_open:
                    self.open_hud()
                else:
                    self.collapse_all()
            elif act == "open":
                self.open_hud()
            elif act == "close":
                self.collapse_all()
            elif act == "summon":
                self.cursor_dismissed = False
                self.pill.sync_mode()
                self._repaint_canvases()
            elif act == "quit":
                self.quit_app()

    def _tick(self) -> None:
        if self.capture_hidden or not self.cursor_visible:
            return
        dirty = False
        dt = 0.016

        if not self.dragging:
            dx = self.target_x - self.render_x
            dy = self.target_y - self.render_y
            dist = math.hypot(dx, dy)
            if dist > 0.35 or math.hypot(self.vel_x, self.vel_y) > 0.35:
                stiffness = 220.0
                damping = 24.0
                ax = stiffness * dx - damping * self.vel_x
                ay = stiffness * dy - damping * self.vel_y
                self.vel_x += ax * dt
                self.vel_y += ay * dt
                old_x, old_y = self.render_x, self.render_y
                self.render_x += self.vel_x * dt
                self.render_y += self.vel_y * dt
                step_dx = self.render_x - old_x
                step_dy = self.render_y - old_y
                speed = math.hypot(step_dx, step_dy)
                target_tilt = max(-18.0, min(18.0, step_dx * 0.42 - step_dy * 0.10))
                self.velocity_tilt = self.velocity_tilt * 0.72 + target_tilt * 0.28
                self.velocity_stretch = 1.0 + min(0.09, speed * 0.0022)
                if self.show_trail and speed > 0.9:
                    self.trail_points.append([self.render_x, self.render_y, 1.0])
                    if len(self.trail_points) > 14:
                        self.trail_points.pop(0)
                self.in_flight = dist > 0.6
                dirty = True
            else:
                if abs(self.velocity_tilt) > 0.05 or abs(self.velocity_stretch - 1.0) > 0.002:
                    self.velocity_tilt *= 0.72
                    self.velocity_stretch = 1.0 + (self.velocity_stretch - 1.0) * 0.72
                    dirty = True
                if self.in_flight:
                    self.render_x = self.target_x
                    self.render_y = self.target_y
                    self.vel_x = 0.0
                    self.vel_y = 0.0
                    self.in_flight = False
                    dirty = True

        # Gentle idle floating motion when idle
        if not self.in_flight and not self.dragging and self.status == "idle" and not self.expanded:
            self._idle_phase += dt * 2.8
            self.idle_float_y = math.sin(self._idle_phase) * 1.7
            dirty = True

        if abs(self.click_scale - 1.0) > 0.005:
            self.click_scale = 1.0 + (self.click_scale - 1.0) * 0.72
            dirty = True

        if self.ripple_life > 0.0:
            self.ripple_life = max(0.0, self.ripple_life - 0.08)
            dirty = True

        if self.trail_points:
            for pt in self.trail_points:
                pt[2] = max(0.0, pt[2] - 0.075)
            self.trail_points = [pt for pt in self.trail_points if pt[2] > 0.02]
            dirty = True

        for clone in self.clones.values():
            cdx = clone["tx"] - clone["rx"]
            cdy = clone["ty"] - clone["ry"]
            if math.hypot(cdx, cdy) > 0.5:
                clone["rx"] += cdx * 0.28
                clone["ry"] += cdy * 0.28
                dirty = True
            if clone["ripple"] > 0.0:
                clone["ripple"] = max(0.0, clone["ripple"] - 0.08)
                dirty = True

        if dirty:
            self._reposition_pill()
            self._repaint_canvases()

    def quit_app(self) -> None:
        self.tick_timer.stop()
        self.hotkey_win.unregister()
        self.bridge.shutdown()
        self.tray.hide()
        self.app.quit()


def run_overlay_app(start_dismissed: bool = False) -> int:
    wb.enable_dpi_awareness()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Kursor AI Agent")

    bridge = AgentBridge()
    coord = OverlayCoordinator(app, bridge)
    bridge.start()
    if start_dismissed:
        coord.dismiss_cursor()

    # Keep screen topology up to date if monitors connect/disconnect
    app.screenAdded.connect(lambda _s: coord._rebuild_canvases())
    app.screenRemoved.connect(lambda _s: coord._rebuild_canvases())

    return app.exec()
