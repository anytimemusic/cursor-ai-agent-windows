from __future__ import annotations

import io
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .win32_backend import find_tesseract

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

_UIA_INTERACTIVE_CONTROLS = {
    "ButtonControl",
    "CheckBoxControl",
    "ComboBoxControl",
    "EditControl",
    "HyperlinkControl",
    "ListItemControl",
    "MenuItemControl",
    "RadioButtonControl",
    "TabItemControl",
    "TreeItemControl",
    "SplitButtonControl",
    "ToggleButton",
}


@dataclass
class UiElement:
    id: int
    text: str
    role: str
    source: str
    x: int
    y: int
    box: tuple[int, int, int, int]
    confidence: float = 100.0

    def to_compact_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "role": self.role,
            "x": self.x,
            "y": self.y,
            "src": self.source,
        }


def _clamp_norm(val: float) -> int:
    return max(0, min(1000, int(round(val))))


def _norm_box(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    total_w: float,
    total_h: float,
) -> tuple[int, int, tuple[int, int, int, int]]:
    nx1 = _clamp_norm((x1 / max(1.0, total_w)) * 1000.0)
    ny1 = _clamp_norm((y1 / max(1.0, total_h)) * 1000.0)
    nx2 = _clamp_norm((x2 / max(1.0, total_w)) * 1000.0)
    ny2 = _clamp_norm((y2 / max(1.0, total_h)) * 1000.0)
    cx = _clamp_norm((nx1 + nx2) * 0.5)
    cy = _clamp_norm((ny1 + ny2) * 0.5)
    return cx, cy, (nx1, ny1, max(nx1 + 1, nx2), max(ny1 + 1, ny2))


def _collect_uia_elements(
    origin_x: int,
    origin_y: int,
    logical_w: int,
    logical_h: int,
    budget_ms: int = 180,
    max_nodes: int = 600,
) -> list[UiElement]:
    """Walk foreground Windows UI Automation tree on a COM-initialized thread."""
    if sys.platform != "win32":
        return []
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return []

    results: list[UiElement] = []
    deadline = time.monotonic() + (budget_ms / 1000.0)
    visited = 0

    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            fg = auto.GetForegroundControl()
            root = fg if fg is not None else auto.GetRootControl()
            if root is None:
                return []
            stack: list[tuple[Any, int]] = [(root, 0)]
            while stack and visited < max_nodes and time.monotonic() < deadline:
                node, depth = stack.pop()
                visited += 1
                if depth > 10:
                    continue
                try:
                    if node.IsOffscreen:
                        continue
                except Exception:
                    pass
                try:
                    ctype = str(getattr(node, "ControlTypeName", "") or "")
                    rect = node.BoundingRectangle
                except Exception:
                    rect = None
                    ctype = ""

                if rect is not None and (ctype in _UIA_INTERACTIVE_CONTROLS):
                    ew = int(rect.right - rect.left)
                    eh = int(rect.bottom - rect.top)
                    if ew >= 6 and eh >= 6:
                        oversized = ew > logical_w * 0.65 and eh > logical_h * 0.65
                        cx_abs = rect.left + ew * 0.5
                        cy_abs = rect.top + eh * 0.5
                        in_mon = (
                            origin_x <= cx_abs < origin_x + logical_w
                            and origin_y <= cy_abs < origin_y + logical_h
                        )
                        if in_mon and not oversized:
                            label = str(getattr(node, "Name", "") or "").strip()
                            if label:
                                lx1 = max(0.0, float(rect.left - origin_x))
                                ly1 = max(0.0, float(rect.top - origin_y))
                                lx2 = min(float(logical_w), float(rect.right - origin_x))
                                ly2 = min(float(logical_h), float(rect.bottom - origin_y))
                                cx, cy, box = _norm_box(lx1, ly1, lx2, ly2, float(logical_w), float(logical_h))
                                results.append(
                                    UiElement(
                                        id=0,
                                        text=label[:48],
                                        role=ctype.replace("Control", "").lower() or "control",
                                        source="uia",
                                        x=cx,
                                        y=cy,
                                        box=box,
                                        confidence=100.0,
                                    )
                                )
                try:
                    children = node.GetChildren()
                    for ch in reversed(children[:50]):
                        stack.append((ch, depth + 1))
                except Exception:
                    pass
    except Exception:
        return results

    return results


def _collect_ocr_elements(rgb: Image.Image, max_ocr_width: int = 1280) -> list[UiElement]:
    tess = find_tesseract()
    if not tess:
        return []

    src = rgb
    if max_ocr_width > 0 and rgb.width > max_ocr_width:
        nh = max(1, round(rgb.height * (max_ocr_width / rgb.width)))
        src = rgb.resize((max_ocr_width, nh), Image.Resampling.BILINEAR)

    ow, oh = float(src.width), float(src.height)
    buf = io.BytesIO()
    src.save(buf, format="PPM")

    try:
        proc = subprocess.run(
            [tess, "stdin", "stdout", "-l", "eng", "--psm", "11", "tsv"],
            input=buf.getvalue(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return []

    if proc.returncode != 0:
        return []

    lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
    raw_words: list[tuple[tuple[int, int, int], str, int, int, int, int, float]] = []
    for ln in lines[1:]:
        cols = ln.split("\t")
        if len(cols) < 12:
            continue
        txt = cols[11].strip()
        if not txt:
            continue
        try:
            conf = float(cols[10])
            block = int(cols[2])
            par = int(cols[3])
            line_no = int(cols[4])
            wx, wy, ww, wh = int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9])
        except ValueError:
            continue
        if conf < 45.0 or ww < 4 or wh < 6 or wh > oh * 0.25 or ww > ow * 0.85:
            continue
        if not any(ch.isalnum() for ch in txt):
            continue
        if len(txt) == 1 and conf < 78.0:
            continue
        raw_words.append(((block, par, line_no), txt, wx, wy, wx + ww, wy + wh, conf))

    phrases: list[list[Any]] = []
    for key, txt, x1, y1, x2, y2, conf in raw_words:
        lh = max(10, y2 - y1)
        ym = (y1 + y2) * 0.5
        if phrases:
            prev = phrases[-1]
            p_key, p_words, px1, py1, px2, py2, p_confs = prev
            p_ym = (py1 + py2) * 0.5
            p_lh = max(10, py2 - py1)
            max_gap = max(20, max(lh, p_lh) * 2.2)
            if p_key == key and 0 <= (x1 - px2) <= max_gap and abs(ym - p_ym) <= max(lh, p_lh) * 0.75:
                p_words.append(txt)
                prev[2] = min(px1, x1)
                prev[3] = min(py1, y1)
                prev[4] = max(px2, x2)
                prev[5] = max(py2, y2)
                p_confs.append(conf)
                continue
        phrases.append([key, [txt], x1, y1, x2, y2, [conf]])

    elements: list[UiElement] = []
    for _, wlist, x1, y1, x2, y2, confs in phrases:
        phrase = " ".join(wlist).strip()
        if not phrase or not any(ch.isalnum() for ch in phrase):
            continue
        avg_conf = sum(confs) / len(confs)
        if len(phrase) == 1 and not phrase.isdigit() and avg_conf < 85.0:
            continue
        cx, cy, box = _norm_box(float(x1), float(y1), float(x2), float(y2), ow, oh)
        elements.append(
            UiElement(
                id=0,
                text=phrase[:48],
                role="ocr_text",
                source="ocr",
                x=cx,
                y=cy,
                box=box,
                confidence=round(avg_conf, 1),
            )
        )
    return elements


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w]+", " ", (s or "").lower())).strip()


def _merge_and_dedup(uia_els: list[UiElement], ocr_els: list[UiElement], limit: int = 75) -> list[UiElement]:
    kept: list[UiElement] = []
    for el in uia_els + ocr_els:
        el_clean = _clean_text(el.text)
        dup = False
        for k in kept:
            if abs(k.x - el.x) <= 22 and abs(k.y - el.y) <= 14:
                k_clean = _clean_text(k.text)
                if not el_clean or el_clean == k_clean or el_clean in k_clean or k_clean in el_clean:
                    dup = True
                    break
        if not dup:
            kept.append(el)

    kept.sort(key=lambda e: (e.y // 16, e.x))
    trimmed = kept[:limit]
    for idx, el in enumerate(trimmed, start=1):
        el.id = idx
    return trimmed


def scan_ui_elements(
    rgb: Image.Image,
    origin_x: int,
    origin_y: int,
    logical_w: int,
    logical_h: int,
    limit: int = 75,
) -> list[UiElement]:
    """Scan Windows UI Automation interactive widgets and OCR text phrases in parallel on 0..1000 coords."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_uia = pool.submit(_collect_uia_elements, origin_x, origin_y, logical_w, logical_h)
        fut_ocr = pool.submit(_collect_ocr_elements, rgb)
        uia_els = fut_uia.result()
        ocr_els = fut_ocr.result()
    return _merge_and_dedup(uia_els, ocr_els, limit=limit)


def draw_som_marks(im: Image.Image, elements: list[UiElement]) -> Image.Image:
    """Draw compact Set-of-Mark bounding outlines and non-colliding numbered [id] badges."""
    if not elements:
        return im
    base = im.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    w, h = base.size

    placed_badges: list[tuple[int, int, int, int]] = []
    for el in elements:
        bx1 = max(0, min(w - 2, round((el.box[0] / 1000.0) * w)))
        by1 = max(0, min(h - 2, round((el.box[1] / 1000.0) * h)))
        bx2 = max(bx1 + 2, min(w - 1, round((el.box[2] / 1000.0) * w)))
        by2 = max(by1 + 2, min(h - 1, round((el.box[3] / 1000.0) * h)))

        if el.source in ("uia", "atspi"):
            border_col = (52, 211, 153, 175)
            badge_bg = (6, 78, 59, 215)
            badge_fg = (167, 243, 208, 255)
        else:
            border_col = (251, 191, 36, 145)
            badge_bg = (24, 24, 27, 215)
            badge_fg = (253, 224, 71, 255)

        draw.rectangle([bx1, by1, bx2, by2], outline=border_col, width=1)

        tag = str(el.id)
        tw = len(tag) * 6 + 4
        th = 11

        candidates = [
            (max(0, min(w - tw, bx1)), max(0, by1 - th - 1)),
            (max(0, min(w - tw, bx2 - tw)), max(0, by1 - th - 1)),
            (max(0, min(w - tw, bx1)), min(h - th, by2 + 1)),
        ]
        chosen: tuple[int, int, int, int] | None = None
        for tx, ty in candidates:
            rect = (tx, ty, tx + tw, ty + th)
            collides = any(
                not (rect[2] <= px1 or rect[0] >= px2 or rect[3] <= py1 or rect[1] >= py2)
                for px1, py1, px2, py2 in placed_badges
            )
            if not collides:
                chosen = rect
                break

        if chosen is not None:
            tx1, ty1, tx2, ty2 = chosen
            placed_badges.append(chosen)
            draw.rectangle([tx1, ty1, tx2, ty2], fill=badge_bg, outline=border_col, width=1)
            draw.text((tx1 + 2, ty1), tag, fill=badge_fg, font=font)

    return Image.alpha_composite(base, overlay).convert("RGB")


def match_elements(elements: list[UiElement], query: str) -> list[UiElement]:
    """Rank UI elements matching a target label or phrase."""
    q_raw = (query or "").strip().lower()
    q_clean = _clean_text(query)
    if not q_clean and not q_raw:
        return []
    q_tokens = set(q_clean.split()) if q_clean else set()

    scored: list[tuple[float, UiElement]] = []
    for el in elements:
        t_raw = el.text.lower().strip()
        t_clean = _clean_text(el.text)
        t_tokens = set(t_clean.split()) if t_clean else set()

        score = 0.0
        if t_raw == q_raw or (q_clean and t_clean == q_clean):
            score = 100.0
        elif q_clean and t_clean.startswith(q_clean):
            score = 88.0
        elif q_raw in t_raw or (q_clean and q_clean in t_clean):
            score = 74.0 + min(10.0, (len(q_clean) / max(1, len(t_clean))) * 10.0)
        elif t_clean and len(t_clean) >= 3 and t_clean in q_clean:
            score = 55.0
        elif q_tokens and t_tokens:
            overlap = len(q_tokens & t_tokens)
            if overlap > 0:
                recall = overlap / len(q_tokens)
                prec = overlap / len(t_tokens)
                if recall >= 0.5 or (len(q_tokens) == 1 and any(q_clean in tt for tt in t_tokens)):
                    score = 35.0 + 30.0 * recall + 10.0 * prec

        if score > 0:
            if el.source in ("uia", "atspi"):
                score += 6.0
            score += (el.confidence / 100.0) * 2.0
            scored.append((score, el))

    scored.sort(key=lambda pair: (-pair[0], pair[1].y, pair[1].x))
    return [pair[1] for pair in scored]
