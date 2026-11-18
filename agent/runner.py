from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .desktop import CloneDesktopProxy, DesktopController
from .llm import (
    LLMTransientError,
    RequestCancelledError,
    _strip_images_from_messages,
    chat_completion,
    list_models,
    pick_default_model,
)
from .tools import CLONE_TOOL_SCHEMAS, TOOL_SCHEMAS, ToolExecutor, ToolResult

_DEAD_MODEL_UNTIL: dict[str, float] = {}

CLONE_COLORS = ("#34d399", "#f59e0b", "#a78bfa", "#fb7185")


def _default_trace_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "cursor-ai-agent" / "trace.log"
    return Path.home() / ".cache" / "cursor-ai-agent" / "trace.log"


TRACE_LOG_PATH = _default_trace_path()


def _write_trace(line: str) -> None:
    try:
        TRACE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with TRACE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {line}\n")
    except Exception:
        pass


SYSTEM_PROMPT = """Kamu adalah Kursor AI — agen desktop cerdas berwujud kursor tambahan interaktif di laptop/PC Windows 11 milik user.

PRINSIP UTAMA, AKURASI & KECEPATAN:
- Screenshot memakai sistem **Set-of-Mark (`[1]`, `[2]`, ...)** (daftar elemen lengkap di `ui_elements` via Windows UI Automation + OCR) **DAN** penggaris grid **0..1000 (normalized)** pada X (0=kiri, 1000=kanan) & Y (0=atas, 1000=bawah).
- **Prioritas Klik Cepat & Akurat**:
  1. Jika tombol/menu/video/kolom target muncul di `ui_elements` atau bertanda `[id]`, LANGSUNG panggil **`click_element(tag_id=...)`** atau **`click_element(text="...")`** (jauh lebih akurat daripada menebak piksel!).
  2. Jika perlu melakukan beberapa klik/ketik/drag berurutan di satu layar, gabungkan menjadi **SATU** panggilan **`run_macro(steps=[...], verify_screen=true)`** untuk memangkas waktu tunggu AI!
  3. Untuk elemen ikon tanpa teks (tidak ada tag `[id]`), baca skala `0..1000` dengan `coord_mode='normalized'`. Untuk cek teks cepat di layar tanpa putaran gambar baru, pakai `find_text(query)`.
- Screenshot awal SUDAH terlampir di pesan pertama user — JANGAN panggil `see_screen` lagi di langkah 1.
- DILARANG berhenti atau bilang selesai sebelum hasil akhirnya benar-benar terlihat di layar (`see_screen` atau `run_macro(verify_screen=true)`).

STRATEGI PINTAR BERDASARKAN JENIS TUGAS:
1. **Membuka Website / URL / Cari di Web**:
   - Langsung `open_url("https://...")` atau `window_action("focuswindow", "...")` -> `run_macro(steps=[{"op":"hotkey","keys":"ctrl+l"},{"op":"type","text":"https://...","press_enter":true},{"op":"wait","seconds":2.5}], verify_screen=true)`.
2. **Membuka / Fokus Aplikasi di Windows 11**:
   - Cek `all_windows`: kalau sudah ada -> `window_action("focuswindow", "address:0x...")`, kalau belum -> `window_action("exec", "nama_app.exe")` + `wait_seconds(1.5)` + `see_screen`.
3. **Tugas Sistem, File, PowerShell, Clipboard**:
   - Pakai `run_shell`, `read_file`, `write_file`, `list_dir`, `clipboard` langsung.
4. **Tugas Cisco Packet Tracer (`packet_tracer_build` & `packet_tracer_configure` — WAJIB DIPAKAI!)**:
   - **Bangun topologi/kabel**: SELALU pakai **`packet_tracer_build(clear_canvas=..., devices=[...], connections=[...])`** — JANGAN menebak toolbar bawah secara manual atau menekan `Ctrl+N` sendiri! Saat membangun topologi baru (`devices` tidak kosong) dan di layar masih ada perangkat lama atau soal meminta kanvas kosong baru, **WAJIB set `clear_canvas=true`**. Mendukung `4321`/`1941`/`2911`, `2960-24TT`/`3560-24PS`, `2504`/`3504` (WLC), `LAP-PT`, `PC-PT`, `Laptop-PT`, `Server-PT`, plus `from_port` & `to_port` (contoh `'G0/1'`, `'G0/0/1'`, `'F0/5'`, `'F0/21'`, `'G1'`) termasuk beberapa kabel paralel untuk EtherChannel.
   - **Fast Forward**: Gunakan `fast_forward=true` di `packet_tracer_build` atau hotkey `alt+d` setelah config—JANGAN klik/hover tombol pojok kiri bawah secara manual lewat `run_macro`.
   - **Config CLI Router/Switch (STP, VLAN, Trunk, EtherChannel, Subinterface/RoaS, OSPF/RIP/Static Route, DHCP, SNMP), WLC Management (`wlc_management`), Server AAA/RADIUS (`radius_server`), Server DHCP (`dhcp_server`), & Host (`PC`/`Laptop`/`Server` IP + `ping`)**: **WAJIB pakai `packet_tracer_configure(devices=[...])`** dalam **1 batch call**! Tool ini otomatis mendeteksi device (`display_name` bisa sekalian diubah di kanvas), menunggu boot, mem-paste IOS CLI, menjalankan verifikasi (`show ip int br`, `show vlan brief`, `show interfaces trunk`, `show spanning-tree`, `show etherchannel summary`), atau membuka GUI (`open_app='browser'` + `browser_url='https://...'` / `open_app='wireless'` + `keep_open=true`) saat perlu klik GUI WLC/WPA2-Enterprise.
   - **DILARANG** mengklik tab `Config`/`CLI`/`Paste` satu per satu secara manual pakai `cursor_click`/`run_macro` atau mengetik CLI manual—itu lambat dan bikin timeout!
   - JANGAN pernah menyambung kabel lewat `cursor_click` terpisah antar giliran—jika pernah menarik kabel via `run_macro`, WAJIB akhiri dengan `{"op":"hotkey","keys":"Escape"}` supaya mode kabel tidak menempel di kursor user!
5. **Cursor Swarm / Belah Diri (`spawn_clones`)**:
   - Gunakan **`spawn_clones(tasks=[...])`** saat user meminta **belah diri / paralel / pakai beberapa kursor**, ATAU saat tugas jelas terdiri dari **2–4 sub-tugas terpisah** (lintas monitor, lintas aplikasi berbeda, atau gabungan GUI + PowerShell/file).
   - Untuk klon tugas shell/file murni, set `attach_screen=false` biar super cepat tanpa OCR.
   - Jangan membelah ke 2 modal dialog berbeda dalam 1 app yang sama—untuk satu jendela modal tetap pakai `run_macro`.
6. **Verifikasi Akhir**: Pastikan hasil akhir terlihat di screenshot `see_screen` / `verify_screen=true`. Akhiri singkat (2-6 kata)."""


def _prune_old_images(messages: list[dict[str, Any]], keep_last: int = 2) -> list[dict[str, Any]]:
    image_indices: list[int] = []
    for i, msg in enumerate(messages):
        c = msg.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            image_indices.append(i)
    drop_set = set(image_indices[:-keep_last]) if len(image_indices) > keep_last else set()
    if not drop_set:
        return messages
    out: list[dict[str, Any]] = []
    for i, msg in enumerate(messages):
        if i in drop_set:
            parts = [p.get("text", "") for p in msg["content"] if isinstance(p, dict) and p.get("type") == "text"]
            new_msg = dict(msg)
            new_msg["content"] = "\n".join(parts) + "\n[Screenshot lama dihapus untuk hemat token]"
            out.append(new_msg)
        else:
            out.append(msg)
    return out


class AgentRunner:
    def __init__(
        self,
        desktop: DesktopController,
        config_getter: Callable[[], dict[str, Any]],
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        self.desktop = desktop
        self.get_config = config_getter
        self.emit = emit

        self._history: list[dict[str, Any]] = []
        self._state_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._paused = False
        self._pause_cond = threading.Condition()
        self._status = "idle"

    @property
    def status(self) -> str:
        return self._status

    def _set_status(self, status: str, label: str = "") -> None:
        self._status = status
        self.emit({"type": "agent_status", "status": status, "label": label})
        if label:
            self.emit({"type": "cursor_status", "label": label})

    def pause_gate(self) -> None:
        was_paused = False
        with self._pause_cond:
            while self._paused and not self._stop_event.is_set():
                was_paused = True
                self.desktop.set_os_cursor_hidden(False)
                self._set_status("paused", "Paused")
                self._pause_cond.wait(timeout=0.25)
        if was_paused and not self._stop_event.is_set():
            with self.desktop._shield_lock:
                if self.desktop._shield_depth > 0:
                    self.desktop.set_os_cursor_hidden(True)

    def pause(self) -> None:
        with self._pause_cond:
            if self._status == "running":
                self._paused = True
                self.desktop.set_os_cursor_hidden(False)
                self._set_status("paused", "Paused")
                self.emit({"type": "log", "level": "warn", "text": "Agent dijeda (Paused)."})

    def resume(self) -> None:
        with self._pause_cond:
            self._paused = False
            self._pause_cond.notify_all()
            if self._worker and self._worker.is_alive():
                self._set_status("running", "Lanjut...")
                self.emit({"type": "log", "level": "info", "text": "Agent dilanjutkan."})

    def stop(self) -> None:
        self._stop_event.set()
        with self._pause_cond:
            self._paused = False
            self._pause_cond.notify_all()
        self.emit({"type": "log", "level": "warn", "text": "Perintah STOP diterima — menghentikan tugas..."})

    def reset_chat(self) -> None:
        if self._worker and self._worker.is_alive():
            self.stop()
            self._worker.join(timeout=2.0)
        self._history = []
        self._set_status("idle", "Siap")
        self.emit({"type": "chat_reset"})

    def run_prompt(self, prompt: str, attach_screen: bool = False) -> bool:
        prompt = prompt.strip()
        if not prompt:
            return False
        old = self._worker
        if old is not None and old.is_alive() and self._stop_event.is_set():
            old.join(timeout=0.8)
        with self._state_lock:
            if self._worker and self._worker.is_alive():
                self.emit({"type": "log", "level": "warn", "text": "Agent masih jalan. Pause/Stop dulu sebelum perintah baru."})
                return False
            self._stop_event.clear()
            with self._pause_cond:
                self._paused = False
            self._set_status("running", "Mikir...")
            self._worker = threading.Thread(
                target=self._run_loop,
                args=(prompt, attach_screen),
                daemon=True,
            )
            self._worker.start()
            return True

    def _resolve_model(self, cfg: dict[str, Any], need_vision: bool = False) -> str:
        base_url = str(cfg.get("base_url") or "")
        api_key = str(cfg.get("api_key") or "")
        model = str(cfg.get("model") or "").strip()
        now = time.monotonic()

        def _is_healthy(m: str) -> bool:
            return now >= _DEAD_MODEL_UNTIL.get(m, 0.0)

        if need_vision and model and ("20128" in base_url) and ("claude" in model.lower()):
            res = list_models(base_url, api_key, timeout=4.0)
            healthy_gem = [m for m in res.get("models", []) if "gemini" in m.lower() and _is_healthy(m)]
            fallback = pick_default_model(healthy_gem)
            if fallback:
                _write_trace(f"WARN vision blind model {model!r} -> switched to {fallback!r}")
                self.emit({"type": "log", "level": "warn", "text": f"{model} di 9router tidak melihat layar; otomatis pakai {fallback}."})
                self._set_status("running", f"Pakai {fallback.split('/')[-1]}...")
                return fallback

        if model:
            if "20128" in base_url and not _is_healthy(model):
                avail = list_models(base_url, api_key, timeout=4.0).get("models", [])
                for cand in ("ag/gemini-3.8-flash-medium", "ag/gemini-3.8-flash-low", "ag/claude-sonnet-4-6", "ag/claude-opus-4-6-thinking", "ag/gpt-oss-120b-medium"):
                    if cand != model and cand in avail and _is_healthy(cand):
                        self.emit({"type": "log", "level": "warn", "text": f"{model} baru saja timeout; otomatis pakai {cand}."})
                        return cand
            return model
        res = list_models(base_url, api_key, timeout=5.0)
        if not res["ok"]:
            raise RuntimeError(
                f"Tidak bisa konek ke {res['base_url']}: {res['error']}. "
                "Pastikan 9router sudah jalan (misal `npx 9router`) atau cek Settings."
            )
        picked = pick_default_model(res["models"])
        if not picked:
            raise RuntimeError(
                f"Terhubung ke {res['base_url']}, tapi daftar model kosong. "
                "Tambahkan/aktifkan minimal 1 model atau provider di dashboard 9router."
            )
        self.emit({"type": "model_auto_selected", "model": picked, "models": res["models"]})
        return picked

    def _execute_llm_loop(
        self,
        desktop: DesktopController,
        history: list[dict[str, Any]],
        sys_prompt: str,
        cfg: dict[str, Any],
        model: str,
        max_steps: int,
        tools_schema: list[dict[str, Any]],
        spawn_clones_fn: Any = None,
        is_clone: bool = False,
        trace_prefix: str = "",
    ) -> tuple[str, str]:
        base_url = str(cfg.get("base_url") or "http://localhost:20128/v1")
        api_key = str(cfg.get("api_key") or "")
        executor = ToolExecutor(
            desktop,
            self.get_config,
            self._stop_event,
            self.pause_gate,
            spawn_clones_fn=spawn_clones_fn,
        )

        did_gui_modify = False
        last_step_was_verify = False
        nudge_count = 0
        incomplete_reason = ""
        final_text = ""
        last_click_sig: tuple[int, int] | None = None
        repeat_click_count = 0

        for step in range(1, max_steps + 1):
            self.pause_gate()
            if self._stop_event.is_set():
                break

            if is_clone:
                desktop.emit({"type": "cursor_status", "label": f"Step {step}..."})
            else:
                self._set_status("running", f"Step {step}...")

            is_9router = "20128" in base_url
            turn_timeout = 25.0 if is_9router else 50.0
            is_blind_9router = is_9router and any(k in model.lower() for k in ("claude", "gpt-oss"))
            sys_use = sys_prompt
            turn_hist = _prune_old_images(history, keep_last=2)
            if is_blind_9router:
                sys_use += "\n\n[CATATAN SISTEM: Model ini pada 9router tidak menerima attachment image_url; gunakan data teks ui_elements (Set-of-Mark), active_window, find_text, packet_tracer_build, dan packet_tracer_configure secara langsung!]"
                turn_hist = _strip_images_from_messages(turn_hist)
            msgs = [{"role": "system", "content": sys_use}] + turn_hist

            try:
                resp = chat_completion(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=msgs,
                    tools=tools_schema,
                    timeout=turn_timeout,
                    cancel_event=self._stop_event,
                )
            except RequestCancelledError:
                break
            except LLMTransientError as err:
                _DEAD_MODEL_UNTIL[model] = time.monotonic() + 180.0
                if not is_9router:
                    raise RuntimeError(str(err)) from err

                avail = list_models(base_url, api_key, timeout=4.0).get("models", [])
                now_ts = time.monotonic()
                failover_order = (
                    "ag/gemini-3.8-flash-medium",
                    "ag/gemini-3.8-flash-low",
                    "ag/claude-sonnet-4-6",
                    "ag/claude-opus-4-6-thinking",
                    "ag/gpt-oss-120b-medium",
                )
                cands = [
                    c for c in failover_order
                    if c != model and c in avail and now_ts >= _DEAD_MODEL_UNTIL.get(c, 0.0)
                ]
                if not cands:
                    raise RuntimeError(str(err)) from err

                resp = None  # type: ignore[assignment]
                last_err: Exception = err
                for cand in cands:
                    _write_trace(f"{trace_prefix}WARN model {model!r} transient fail -> trying {cand!r}: {last_err}")
                    if not is_clone:
                        self.emit({"type": "log", "level": "warn", "text": f"{model} gagal/timeout; pindah ke {cand}."})
                        self._set_status("running", f"Pakai {cand.split('/')[-1]}...")
                    cand_blind = any(k in cand.lower() for k in ("claude", "gpt-oss"))
                    cand_sys = sys_prompt + (
                        "\n\n[CATATAN SISTEM: Model cadangan ini tidak menerima image_url; gunakan ui_elements (Set-of-Mark), active_window, find_text, packet_tracer_build, dan packet_tracer_configure secara penuh!]"
                        if cand_blind
                        else ""
                    )
                    cand_msgs = [{"role": "system", "content": cand_sys}] + (_strip_images_from_messages(turn_hist) if cand_blind else turn_hist)
                    try:
                        resp = chat_completion(
                            base_url=base_url,
                            api_key=api_key,
                            model=cand,
                            messages=cand_msgs,
                            tools=tools_schema,
                            timeout=25.0,
                            cancel_event=self._stop_event,
                        )
                        model = cand
                        break
                    except RequestCancelledError:
                        resp = None  # type: ignore[assignment]
                        break
                    except LLMTransientError as e2:
                        _DEAD_MODEL_UNTIL[cand] = time.monotonic() + 180.0
                        last_err = e2
                if self._stop_event.is_set():
                    break
                if resp is None:
                    raise RuntimeError(str(last_err)) from last_err

            if self._stop_event.is_set():
                break

            assistant_msg: dict[str, Any] = {"role": "assistant", "content": resp.content}
            if resp.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in resp.tool_calls
                ]
            history.append(assistant_msg)

            if resp.content:
                final_text = resp.content
                _write_trace(f"{trace_prefix}STEP {step} TEXT: {resp.content[:240]}")
                if not is_clone:
                    self.emit({"type": "assistant_message", "text": resp.content, "final": not bool(resp.tool_calls)})

            if not resp.tool_calls:
                low_txt = (resp.content or "").lower()
                looks_incomplete = any(
                    kw in low_txt
                    for kw in ("tidak bisa", "kesulitan", "gagal", "maaf", "silakan", "belum ", "unable", "cannot", "failed", "sorry", "not possible")
                )
                needs_verify = did_gui_modify and not last_step_was_verify
                if (looks_incomplete or needs_verify) and nudge_count < (1 if is_clone else 2) and step < max_steps:
                    nudge_count += 1
                    _write_trace(f"{trace_prefix}STEP {step} NUDGE #{nudge_count} (incomplete={looks_incomplete}, needs_verify={needs_verify})")
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "Jangan berhenti sebelum tugas benar-benar jadi dan terverifikasi di layar. "
                                "Panggil `see_screen(monitor='focused')` sekarang untuk cek kondisi layar. "
                                "Gunakan koordinat 0..1000 normalized sesuai angka grid/penggaris sampai tugas benar-benar selesai!"
                            ),
                        }
                    )
                    continue
                if looks_incomplete:
                    incomplete_reason = resp.content[:140]
                break

            pending_images: list[tuple[str, str]] = []
            for tc in resp.tool_calls:
                self.pause_gate()
                if self._stop_event.is_set():
                    break
                if tc.name in ("cursor_click", "click_element", "cursor_drag", "type_text", "press_hotkey", "run_macro", "spawn_clones", "packet_tracer_build", "packet_tracer_configure"):
                    did_gui_modify = True
                    last_step_was_verify = False
                elif tc.name == "see_screen":
                    last_step_was_verify = True
                arg_preview = json.dumps(tc.arguments, ensure_ascii=False)[:140]
                _write_trace(f"{trace_prefix}STEP {step} TOOL {tc.name} args={arg_preview}")
                if not is_clone:
                    self.emit({"type": "tool_start", "name": tc.name, "args": arg_preview})
                try:
                    res: ToolResult = executor.execute(tc.name, tc.arguments)
                except Exception as e:
                    res = ToolResult(text=json.dumps({"error": str(e)}, ensure_ascii=False))
                    _write_trace(f"{trace_prefix}STEP {step} TOOL_ERR {tc.name}: {e}")

                if (res.image_data_url and tc.name in ("see_screen", "run_macro", "packet_tracer_build")) or tc.name == "packet_tracer_configure":
                    last_step_was_verify = True
                _write_trace(f"{trace_prefix}STEP {step} RESULT {tc.name}: {res.text[:220]}")
                if not is_clone:
                    self.emit({"type": "tool_end", "name": tc.name, "result": res.text[:240]})
                tool_text = res.text
                if tc.name == "cursor_click":
                    sig = (round(float(tc.arguments.get("x", 0))), round(float(tc.arguments.get("y", 0))))
                    if last_click_sig and abs(sig[0] - last_click_sig[0]) <= 8 and abs(sig[1] - last_click_sig[1]) <= 8:
                        repeat_click_count += 1
                        if repeat_click_count >= 1:
                            tool_text += f"\n[SYSTEM HINT: You clicked near ({sig[0]}, {sig[1]}) repeatedly. Look at the red crosshair in see_screen, try click_element/find_text, or pick a NEW coordinate!]"
                    else:
                        repeat_click_count = 0
                    last_click_sig = sig
                history.append({"role": "tool", "tool_call_id": tc.id, "content": tool_text})
                if res.image_data_url and cfg.get("vision_enabled", True):
                    pending_images.append((tc.name, res.image_data_url))

            if pending_images and not self._stop_event.is_set():
                content_parts: list[dict[str, Any]] = [
                    {"type": "text", "text": "[Visual screen capture from tool call]"}
                ]
                for _, img_url in pending_images[-1:]:
                    content_parts.append({"type": "image_url", "image_url": {"url": img_url}})
                history.append({"role": "user", "content": content_parts})

            if step == max_steps:
                _write_trace(f"{trace_prefix}MAX_STEPS reached ({max_steps})")
                incomplete_reason = f"Mencapai batas {max_steps} langkah"
                if not is_clone:
                    self.emit({"type": "log", "level": "warn", "text": f"Mencapai batas {max_steps} langkah."})

        return final_text, incomplete_reason

    def _run_clones(
        self,
        tasks: list[dict[str, Any]],
        cfg: dict[str, Any],
        model: str,
    ) -> list[dict[str, Any]]:
        specs = tasks[:4]
        mons = self.desktop.get_monitors()
        focused = next((m for m in mons if m.get("focused")), mons[0] if mons else None)
        mx, my = self.desktop.ai_cursor_x, self.desktop.ai_cursor_y
        fan_offsets = [(-130, -85), (130, -85), (-130, 85), (130, 85)]

        clone_defs: list[dict[str, Any]] = []
        snap_cache: dict[str, tuple[str, Any, list[Any]]] = {}
        use_som = bool(cfg.get("som_enabled", True))
        shot_w = int(cfg.get("screenshot_width", 1440))

        for idx, spec in enumerate(specs):
            cid = f"clone-{idx + 1}"
            title = str(spec.get("title") or f"Klon #{idx + 1}").strip()[:20]
            instr = str(spec.get("instruction") or "").strip()
            mon_name = str(spec.get("monitor") or "focused").strip()
            attach = bool(spec.get("attach_screen", True))
            color = CLONE_COLORS[idx % len(CLONE_COLORS)]

            target_mon = next((m for m in mons if str(m["name"]).lower() == mon_name.lower()), focused) if mon_name != "focused" else focused
            dx, dy = fan_offsets[idx % len(fan_offsets)]
            if target_mon:
                base_x = int(target_mon["x"]) + int(target_mon["logical_w"]) // 2 if (target_mon is not focused and focused) else mx
                base_y = int(target_mon["y"]) + int(target_mon["logical_h"]) // 2 if (target_mon is not focused and focused) else my
                tx = max(int(target_mon["x"]) + 60, min(int(target_mon["x"]) + int(target_mon["logical_w"]) - 140, base_x + dx))
                ty = max(int(target_mon["y"]) + 60, min(int(target_mon["y"]) + int(target_mon["logical_h"]) - 100, base_y + dy))
            else:
                tx, ty = mx + dx, my + dy

            if attach and cfg.get("vision_enabled", True):
                cache_key = str(target_mon["name"]) if target_mon else mon_name
                if cache_key not in snap_cache:
                    snap_cache[cache_key] = self.desktop.scan_and_capture(
                        mon_name,
                        shot_w,
                        draw_grid=True,
                        draw_som=use_som,
                    )

            clone_defs.append(
                {
                    "id": cid,
                    "idx": idx + 1,
                    "title": title,
                    "instruction": instr,
                    "monitor": mon_name,
                    "cache_key": str(target_mon["name"]) if target_mon else mon_name,
                    "attach": attach,
                    "color": color,
                    "x": mx,
                    "y": my,
                    "target_x": tx,
                    "target_y": ty,
                }
            )

        self._set_status("running", f"Belah {len(clone_defs)} kursor...")
        self.desktop.emit(
            {
                "type": "clone_spawn",
                "clones": [
                    {
                        "id": c["id"],
                        "x": c["x"],
                        "y": c["y"],
                        "target_x": c["target_x"],
                        "target_y": c["target_y"],
                        "color": c["color"],
                        "label": c["title"],
                    }
                    for c in clone_defs
                ],
            }
        )
        time.sleep(0.20)

        clone_max_steps = max(3, min(14, int(cfg.get("max_steps", 45))))

        def _worker(c: dict[str, Any]) -> dict[str, Any]:
            cid = c["id"]
            proxy = CloneDesktopProxy(self.desktop, cid, c["target_x"], c["target_y"])
            clone_history: list[dict[str, Any]] = []
            if c["attach"] and cfg.get("vision_enabled", True) and c["cache_key"] in snap_cache:
                b64, meta, els = snap_cache[c["cache_key"]]
                proxy.last_shot = meta
                proxy.last_elements = list(els)
                proxy.last_elements_monitor = meta.monitor
                ctx_json = json.dumps(proxy.build_screen_context(meta, els if use_som else None), ensure_ascii=False)
                clone_history.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Sub-tugas Klon ({c['title']}):\n{c['instruction']}\n\n[Auto screen context]\n{ctx_json}",
                            },
                            {"type": "image_url", "image_url": {"url": b64}},
                        ],
                    }
                )
            else:
                clone_history.append({"role": "user", "content": f"Sub-tugas Klon ({c['title']}):\n{c['instruction']}"})

            clone_sys = (
                SYSTEM_PROMPT
                + f"\n\nPERAN SWARM: Kamu adalah {cid} ('{c['title']}'). Selesaikan HANYA sub-tugasmu dengan cepat & tepat. "
                "Jangan kerjakan sub-tugas klon lain. Aksi input fisik otomatis diantre aman dengan kunci."
            )
            ok = False
            err_msg = ""
            summary = ""
            try:
                summary, inc = self._execute_llm_loop(
                    desktop=proxy,
                    history=clone_history,
                    sys_prompt=clone_sys,
                    cfg=cfg,
                    model=model,
                    max_steps=clone_max_steps,
                    tools_schema=CLONE_TOOL_SCHEMAS,
                    spawn_clones_fn=None,
                    is_clone=True,
                    trace_prefix=f"[{cid}] ",
                )
                ok = (not self._stop_event.is_set()) and (not inc)
                if inc:
                    err_msg = inc
            except Exception as exc:
                err_msg = str(exc)
            finally:
                end_lbl = "Menyatu ✓" if ok else "Menyatu!"
                self.desktop.emit({"type": "clone_status", "id": cid, "label": end_lbl})
                self.desktop.emit(
                    {
                        "type": "clone_move",
                        "id": cid,
                        "x": self.desktop.ai_cursor_x,
                        "y": self.desktop.ai_cursor_y,
                        "label": end_lbl,
                    }
                )
            return {"clone": cid, "title": c["title"], "ok": ok, "summary": summary[:220], "error": err_msg}

        results: list[dict[str, Any]] = []
        try:
            with ThreadPoolExecutor(max_workers=len(clone_defs)) as pool:
                results = list(pool.map(_worker, clone_defs))
            if not self._stop_event.is_set():
                time.sleep(0.28)
        finally:
            self.desktop.emit({"type": "clone_merge"})
            self._set_status("running", "Menyatu ✓")
        return results

    def _run_loop(self, prompt: str, attach_screen: bool) -> None:
        cfg = self.get_config()
        restore_cursor = bool(cfg.get("restore_cursor", True))
        self.desktop.begin_task()
        _write_trace(f"START prompt={prompt!r} model={cfg.get('model')!r}")
        incomplete_reason = ""
        try:
            self._set_status("running", "Mikir...")
            self.emit({"type": "user_message", "text": prompt})
            max_steps = max(1, min(80, int(cfg.get("max_steps", 45))))
            model = self._resolve_model(cfg, need_vision=bool(attach_screen and cfg.get("vision_enabled", True)))

            if attach_screen and cfg.get("vision_enabled", True):
                use_som = bool(cfg.get("som_enabled", True))
                data_url, meta, elements = self.desktop.scan_and_capture(
                    "focused",
                    int(cfg.get("screenshot_width", 1440)),
                    draw_grid=True,
                    draw_som=use_som,
                )
                ctx_json = json.dumps(
                    self.desktop.build_screen_context(meta, elements if use_som else None),
                    ensure_ascii=False,
                )
                user_msg: dict[str, Any] = {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{prompt}\n\n[Auto screen context]\n{ctx_json}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            else:
                user_msg = {"role": "user", "content": prompt}

            self._history.append(user_msg)

            sys_prompt = SYSTEM_PROMPT
            extra = str(cfg.get("system_prompt_extra") or "").strip()
            if extra:
                sys_prompt += f"\n\nInstruksi tambahan user:\n{extra}"

            _, incomplete_reason = self._execute_llm_loop(
                desktop=self.desktop,
                history=self._history,
                sys_prompt=sys_prompt,
                cfg=cfg,
                model=model,
                max_steps=max_steps,
                tools_schema=TOOL_SCHEMAS,
                spawn_clones_fn=lambda t: self._run_clones(t, cfg, model),
                is_clone=False,
                trace_prefix="",
            )

            if len(self._history) > 40:
                self._history = self._history[-30:]

        except Exception as e:
            _write_trace(f"ERROR: {e}")
            self.emit({"type": "error", "text": str(e)})
            self._set_status("idle", "Error — klik ⚙")
            incomplete_reason = incomplete_reason or str(e)
        else:
            if self._stop_event.is_set():
                _write_trace("STOPPED by user")
                self._set_status("idle", "Stopped")
                self.emit({"type": "log", "level": "warn", "text": "Tugas dihentikan."})
            elif incomplete_reason:
                _write_trace(f"INCOMPLETE: {incomplete_reason}")
                self.emit({"type": "error", "text": incomplete_reason})
                self._set_status("idle", "Belum selesai")
            else:
                _write_trace("DONE")
                self._set_status("idle", "Selesai ✓")
        finally:
            self.desktop.emit({"type": "clone_merge"})
            self.desktop.end_task(
                restore_cursor=restore_cursor,
                aborted=self._stop_event.is_set() or bool(incomplete_reason),
            )
            if self._stop_event.is_set() and self._status != "idle":
                self._set_status("idle", "Stopped")
