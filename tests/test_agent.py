from __future__ import annotations

import os
import py_compile
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import bridge as bridge_mod
from agent import config as cfg_mod
from agent import desktop as desktop_mod
from agent import llm as llm_mod
from agent import packet_tracer as pt_mod
from agent import perception as perc_mod
from agent import runner as runner_mod
from agent import win32_backend as wb
import cli as cli_mod
import run as run_mod


class TestRepoSyntaxAndCLI(unittest.TestCase):
    def test_all_python_files_compile_cleanly(self) -> None:
        py_files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "agent").glob("*.py")) + sorted((ROOT / "ui").glob("*.py"))
        self.assertGreaterEqual(len(py_files), 12)
        for p in py_files:
            py_compile.compile(str(p), doraise=True)

    def test_cli_payload_builder(self) -> None:
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["status"])), {"cmd": "status"})
        self.assertEqual(
            cli_mod.build_command_payload(cli_mod.parse_args(["ask", "buat", "topologi"])),
            {"cmd": "ask", "prompt": "buat topologi", "attach_screen": True},
        )
        self.assertEqual(
            cli_mod.build_command_payload(cli_mod.parse_args(["ask", "--no-screen", "halo"])),
            {"cmd": "ask", "prompt": "halo", "attach_screen": False},
        )
        for act in ("toggle", "open", "close", "summon"):
            self.assertEqual(
                cli_mod.build_command_payload(cli_mod.parse_args([act])),
                {"cmd": "ui", "action": act},
            )
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["pause"])), {"cmd": "toggle_pause"})
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["halt"])), {"cmd": "stop"})
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["demo"])), {"cmd": "demo"})
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["demo-swarm"])), {"cmd": "demo_swarm"})
        self.assertEqual(
            cli_mod.build_command_payload(cli_mod.parse_args(["move", "420", "310", "--label", "Tes"])),
            {"cmd": "move_cursor", "x": 420, "y": 310, "label": "Tes"},
        )
        self.assertEqual(cli_mod.build_command_payload(cli_mod.parse_args(["quit"])), {"cmd": "quit"})

    def test_run_args(self) -> None:
        self.assertTrue(run_mod.parse_args(["--tray"]).tray)
        self.assertTrue(run_mod.parse_args(["--hidden"]).tray)
        self.assertTrue(run_mod.parse_args(["--headless"]).headless)
        self.assertFalse(run_mod.parse_args([]).tray)


class TestConfig(unittest.TestCase):
    def test_windows_appdata_path_and_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_appdata = Path(tmp) / "Roaming"
            with mock.patch.object(cfg_mod.sys, "platform", "win32"), mock.patch.dict(os.environ, {"APPDATA": str(fake_appdata)}):
                d = cfg_mod.get_config_dir()
                self.assertEqual(d, fake_appdata / "cursor-ai-agent")

            cfg_dir = Path(tmp) / "cfg"
            cfg_file = cfg_dir / "config.json"
            with mock.patch.object(cfg_mod, "CONFIG_DIR", cfg_dir), mock.patch.object(cfg_mod, "CONFIG_PATH", cfg_file):
                initial = cfg_mod.load_config()
                self.assertEqual(initial["provider"], "9router")
                self.assertIn("gemini", cfg_mod.PROVIDER_PRESETS)
                updated = cfg_mod.save_config({"provider": "openrouter", "model": "google/gemini-2.5-flash", "max_steps": 30})
                self.assertEqual(updated["provider"], "openrouter")
                self.assertEqual(updated["model"], "google/gemini-2.5-flash")
                self.assertEqual(updated["max_steps"], 30)
                reloaded = cfg_mod.load_config()
                self.assertEqual(reloaded["model"], "google/gemini-2.5-flash")


class TestWin32BackendHelpers(unittest.TestCase):
    def test_to_sendinput_abs_single_and_multi_monitor(self) -> None:
        nx, ny = wb.to_sendinput_abs(960, 540, 0, 0, 1920, 1080)
        self.assertTrue(32700 <= nx <= 32850)
        self.assertTrue(32700 <= ny <= 32850)

        lx, ly = wb.to_sendinput_abs(-1920, 0, -1920, 0, 3840, 1080)
        self.assertGreaterEqual(lx, 0)
        self.assertLess(lx, 40)
        self.assertLess(ly, 100)

        rx, ry = wb.to_sendinput_abs(1919, 1079, -1920, 0, 3840, 1080)
        self.assertGreater(rx, 65450)
        self.assertLessEqual(rx, 65535)
        self.assertGreater(ry, 65450)

        cx, cy = wb.to_sendinput_abs(-5000, 99999, 0, 0, 1920, 1080)
        self.assertEqual((cx, cy), (0, 65535))

    def test_parse_hotkey_tokens(self) -> None:
        mods, keys = wb.parse_hotkey_tokens("ctrl+shift+6")
        self.assertEqual(len(mods), 2)
        self.assertEqual(keys, [0x36])

        mods2, keys2 = wb.parse_hotkey_tokens("ctrl+alt+space")
        self.assertEqual(len(mods2), 2)
        self.assertEqual(keys2, [0x20])

        mods3, keys3 = wb.parse_hotkey_tokens("Return")
        self.assertEqual(mods3, [])
        self.assertEqual(keys3, [0x0D])

        with self.assertRaises(ValueError):
            wb.parse_hotkey_tokens("ctrl+not_a_real_key_token")

    def test_normalize_win_process_name(self) -> None:
        self.assertEqual(wb.normalize_win_process_name("PacketTracer.exe"), "PacketTracer")
        self.assertEqual(wb.normalize_win_process_name("packettracer8.exe"), "PacketTracer")
        self.assertEqual(wb.normalize_win_process_name("Notepad.exe"), "notepad")
        self.assertEqual(wb.normalize_win_process_name(""), "unknown")

    def test_find_tesseract_windows_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pf = Path(tmp) / "Program Files"
            exe = pf / "Tesseract-OCR" / "tesseract.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
            with (
                mock.patch.object(wb.shutil, "which", return_value=None),
                mock.patch.object(wb, "IS_WINDOWS", True),
                mock.patch.dict(os.environ, {"ProgramFiles": str(pf), "ProgramFiles(x86)": str(tmp), "LOCALAPPDATA": str(tmp)}),
            ):
                self.assertEqual(wb.find_tesseract(), str(exe))


class TestPerception(unittest.TestCase):
    def test_som_and_match_elements(self) -> None:
        els = [
            perc_mod.UiElement(
                id=1,
                text="Configure Terminal",
                role="Button",
                source="uia",
                x=250,
                y=400,
                box=(200, 380, 300, 420),
            ),
            perc_mod.UiElement(
                id=2,
                text="GigabitEthernet0/0/1",
                role="ocr_text",
                source="ocr",
                x=700,
                y=500,
                box=(640, 485, 760, 515),
            ),
        ]
        im = Image.new("RGB", (800, 600), (24, 24, 30))
        annotated = perc_mod.draw_som_marks(im, els)
        self.assertEqual(annotated.size, (800, 600))
        self.assertNotEqual(annotated.tobytes(), im.tobytes())

        by_text = perc_mod.match_elements(els, "gigabitethernet0/0/1")
        self.assertEqual(len(by_text), 1)
        self.assertEqual(by_text[0].id, 2)

        by_sub = perc_mod.match_elements(els, "Terminal")
        self.assertEqual(by_sub[0].id, 1)

    def test_scan_ui_elements_merges_and_dedups(self) -> None:
        uia = [
            perc_mod.UiElement(id=0, text="Save", role="Button", source="uia", x=100, y=100, box=(80, 90, 120, 110)),
        ]
        ocr = [
            perc_mod.UiElement(id=0, text="Save", role="ocr_text", source="ocr", x=102, y=101, box=(80, 90, 120, 110)),
            perc_mod.UiElement(id=0, text="Cancel", role="ocr_text", source="ocr", x=220, y=100, box=(200, 90, 240, 110)),
        ]
        img = Image.new("RGB", (640, 480), (255, 255, 255))
        with (
            mock.patch.object(perc_mod, "_collect_uia_elements", return_value=uia),
            mock.patch.object(perc_mod, "_collect_ocr_elements", return_value=ocr),
        ):
            merged = perc_mod.scan_ui_elements(img, 0, 0, 1920, 1080, limit=20)
        self.assertEqual([e.text for e in merged], ["Save", "Cancel"])
        self.assertEqual([e.id for e in merged], [1, 2])


class TestDesktopController(unittest.TestCase):
    def _make_ctrl(self, events: list[dict]) -> desktop_mod.DesktopController:
        mons = [
            {"name": "DISPLAY1", "x": 0, "y": 0, "logical_w": 1920, "logical_h": 1080, "work_bottom": 1032, "focused": True, "dpi_scale": 1.25},
            {"name": "DISPLAY2", "x": -1920, "y": 0, "logical_w": 1920, "logical_h": 1080, "work_bottom": 1080, "focused": False, "dpi_scale": 1.0},
        ]
        with mock.patch.object(wb, "get_monitors", return_value=mons):
            return desktop_mod.DesktopController(on_event=events.append)

    def test_overlay_input_shield_nested(self) -> None:
        events: list[dict] = []
        ctrl = self._make_ctrl(events)
        with ctrl.overlay_input_shield():
            with ctrl.overlay_input_shield():
                pass
        shield_events = [e for e in events if e.get("type") == "input_shield"]
        self.assertEqual(shield_events, [{"type": "input_shield", "active": True}, {"type": "input_shield", "active": False}])

    def test_resolve_coords_modes_and_secondary_monitor(self) -> None:
        events: list[dict] = []
        ctrl = self._make_ctrl(events)
        ctrl.last_shot = desktop_mod.ScreenshotMeta(
            monitor="DISPLAY2",
            origin_x=-1920,
            origin_y=0,
            logical_w=1920,
            logical_h=1080,
            img_w=1440,
            img_h=810,
        )
        self.assertEqual(ctrl.resolve_coords(500, 500, "normalized"), (-960, 540))
        self.assertEqual(ctrl.resolve_coords(0.25, 0.5, "normalized"), (-1440, 540))
        self.assertEqual(ctrl.resolve_coords(720, 405, "pixel"), (-960, 540))
        self.assertEqual(ctrl.resolve_coords(123, 456, "global"), (123, 456))

    def test_disarm_if_packet_tracer(self) -> None:
        events: list[dict] = []
        ctrl = self._make_ctrl(events)
        with (
            mock.patch.object(ctrl, "get_active_window", return_value={"class": "PacketTracer", "title": "Cisco Packet Tracer - Lab1", "floating": False}),
            mock.patch.object(ctrl, "press_hotkey") as mock_hk,
        ):
            self.assertTrue(ctrl.disarm_if_packet_tracer())
            self.assertEqual(mock_hk.call_count, 2)


class TestPacketTracerWindows(unittest.TestCase):
    def test_detect_pt_calibration_from_array_with_taskbar(self) -> None:
        arr = np.full((1080, 1920, 3), 245, dtype=np.uint8)
        arr[860:910, 40:1900, 0] = 20
        arr[860:910, 40:1900, 1] = 55
        arr[860:910, 40:1900, 2] = 110
        pt_win = {"address": "0x1001", "at": [40, 30], "size": [1860, 990]}

        cal = pt_mod.detect_pt_calibration_from_array(
            arr=arr,
            pt_window=pt_win,
            monitor_name="DISPLAY1",
            mon_origin_x=0,
            mon_origin_y=0,
            work_bottom=1032,
        )
        self.assertEqual(cal.win_left_x, 40)
        self.assertEqual(cal.nav_bottom_y, 910)
        self.assertEqual(cal.work_bottom, 1032)
        self.assertGreater(cal.canvas_x2, cal.canvas_x1)
        self.assertGreater(cal.canvas_y2, cal.canvas_y1)

    def test_windows_popup_qmenu_and_tooltip_classification(self) -> None:
        windows = [
            {"address": "0x10", "class": "PacketTracer", "title": "Cisco Packet Tracer - Lab.pka", "floating": False, "at": [0, 0], "size": [1920, 1032]},
            {"address": "0x20", "class": "PacketTracer", "title": "", "win_class": "Qt6QWindowPopupDropShadowSaveBits", "floating": True, "at": [620, 440], "size": [175, 120]},
            {"address": "0x30", "class": "PacketTracer", "title": "", "win_class": "Qt6QWindowToolTipSaveBits", "floating": True, "at": [610, 430], "size": [90, 24]},
            {"address": "0x40", "class": "PacketTracer", "title": "R1", "win_class": "QDialog", "floating": True, "at": [500, 200], "size": [760, 620]},
        ]
        main_w, children = pt_mod._split_pt_windows(windows)
        self.assertEqual(main_w["address"], "0x10")
        self.assertEqual(len(children), 3)
        self.assertTrue(pt_mod._is_pt_port_menu(children[0]))
        self.assertTrue(pt_mod._is_pt_hover_tooltip(children[1]))
        self.assertFalse(pt_mod._is_pt_port_menu(children[2]))

    def test_select_pt_port_clamps_above_windows_taskbar(self) -> None:
        cal = pt_mod.PTCalibration(
            monitor="DISPLAY1",
            window_address="0x1",
            win_left_x=0,
            win_top_y=0,
            win_w=1920,
            win_h=1032,
            nav_bottom_y=890,
            cat_y=917,
            subcat_y=990,
            search_x=103,
            search_y=954,
            canvas_x1=36,
            canvas_y1=115,
            canvas_x2=1884,
            canvas_y2=864,
            work_bottom=1032,
        )
        menu_win = {"address": "0x99", "class": "PacketTracer", "title": "", "floating": True, "at": [800, 960], "size": [190, 110]}
        main_win = {"address": "0x1", "class": "PacketTracer", "title": "Cisco Packet Tracer", "floating": False, "at": [0, 0], "size": [1920, 1032]}

        mock_desktop = mock.MagicMock()
        mock_desktop.get_windows.side_effect = [
            [main_win, menu_win],
            [main_win],
        ]
        shot = Image.new("RGB", (1920, 1080), (240, 240, 240))
        meta = desktop_mod.ScreenshotMeta("DISPLAY1", 0, 0, 1920, 1080, 1920, 1080)
        ocr_rows = [
            perc_mod.UiElement(1, "Console", "ocr_text", "ocr", 500, 200, (100, 150, 900, 250)),
            perc_mod.UiElement(2, "FastEthernet0/1", "ocr_text", "ocr", 500, 820, (100, 790, 900, 860)),
        ]

        with (
            mock.patch.object(pt_mod, "_capture_clean_monitor", return_value=(shot, meta)),
            mock.patch.object(pt_mod, "_ocr_crop_elements", return_value=ocr_rows),
        ):
            picked = pt_mod._select_pt_port(mock_desktop, cal, (790, 950), "Fa0/1", glide_ms=0, label_prefix="Src")

        self.assertEqual(picked, "FastEthernet0/1")
        # Py for y=820 in menu height 110 at y=960 -> ~1061 > safe_bottom(1029), so must NOT blindly click into taskbar!
        mock_desktop.click.assert_not_called()
        mock_desktop.press_hotkey.assert_any_call("Return")

    def test_port_normalization_device_family_and_ios_script(self) -> None:
        self.assertEqual(pt_mod._normalize_port_query("G0/0/1"), ("gig", "0/0/1"))
        self.assertEqual(pt_mod._normalize_port_query("FaO/21"), ("fast", "0/21"))
        self.assertEqual(pt_mod._normalize_port_query("[GigabitEthernet0/1]"), ("gig", "0/1"))
        self.assertEqual(pt_mod._device_family("2504-WLC"), "wlc")
        self.assertEqual(pt_mod._device_family("2960-24TT"), "switch")
        self.assertEqual(pt_mod._infer_cable_for_pair("2960-24TT", "S1", "2960-24TT", "S2"), "cross")
        self.assertEqual(pt_mod._infer_cable_for_pair("2960-24TT", "S1", "PC-PT", "PC1"), "straight")

        script = pt_mod._normalize_ios_script(
            ["hostname SW1", "vlan 10", "int range fa0/1-2", "channel-group 1 mode active"],
            current_prompt="Switch>",
            verify_cli=True,
        )
        self.assertIn("enable", script)
        self.assertIn("configure terminal", script)
        self.assertIn("no ip domain-lookup", script)
        self.assertIn("show vlan brief", script)
        self.assertIn("show etherchannel summary", script)

    def test_cable_detection(self) -> None:
        before = np.full((300, 400, 3), 255, dtype=np.uint8)
        after = before.copy()
        for i in range(201):
            x = 50 + i
            y = 50 + i // 2
            after[y - 1 : y + 2, x - 1 : x + 2] = [20, 20, 20]
        hits = pt_mod._segment_change_score(before, after, 0, 0, (50, 50), (250, 150))
        self.assertGreaterEqual(hits, 5)
        state = pt_mod._detect_cable_state(before, after, 0, 0, (50, 50), (250, 150), probe=(50, 250))
        self.assertEqual(state, "connected")

        dangling_after = before.copy()
        for y in range(60, 240):
            dangling_after[y, 49:52] = [20, 20, 20]
        d_state = pt_mod._detect_cable_state(before, dangling_after, 0, 0, (50, 50), (250, 150), probe=(50, 250))
        self.assertEqual(d_state, "dangling")


class TestLLMAndRunnerFailover(unittest.TestCase):
    def test_sse_and_image_stripping(self) -> None:
        sse = (
            'data: {"choices":[{"delta":{"content":"Halo "}}]}\n'
            'data: {"choices":[{"delta":{"content":"dunia","tool_calls":[{"index":0,"id":"tc1","function":{"name":"finish","arguments":"{\\"summary\\": \\"ok\\"}"}}]}}]}\n'
            'data: [DONE]\n'
        )
        parsed = llm_mod._parse_sse_chat_completion(sse, fallback_model="ag/test")
        msg = parsed["choices"][0]["message"]
        self.assertEqual(msg["content"], "Halo dunia")
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "finish")

        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "lihat"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}}]},
            {"role": "user", "content": [{"type": "text", "text": "baru"}, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,def"}}]},
        ]
        pruned = runner_mod._prune_old_images(msgs, keep_last=1)
        self.assertIsInstance(pruned[0]["content"], str)
        self.assertIn("Screenshot lama dihapus", pruned[0]["content"])
        self.assertEqual(len(pruned[1]["content"]), 2)
        stripped = llm_mod._strip_images_from_messages(msgs)
        self.assertIsInstance(stripped[0]["content"], str)
        self.assertIn("Screenshot omitted", stripped[0]["content"])
        self.assertIsInstance(stripped[1]["content"], str)

    def test_9router_failover_and_cooldown(self) -> None:
        events: list[dict] = []
        with mock.patch.object(wb, "get_monitors", return_value=[{"name": "D1", "x": 0, "y": 0, "logical_w": 1920, "logical_h": 1080, "focused": True}]):
            desktop = desktop_mod.DesktopController(on_event=events.append)

        cfg = {
            "provider": "9router",
            "base_url": "http://localhost:20128/v1",
            "api_key": "9router",
            "model": "ag/gemini-3.8-flash-medium",
            "max_steps": 4,
            "restore_cursor": False,
        }
        runner = runner_mod.AgentRunner(desktop=desktop, config_getter=lambda: cfg, emit=events.append)

        def _fake_chat(**kwargs):
            m = kwargs["model"]
            if m == "ag/gemini-3.8-flash-medium":
                raise llm_mod.LLMTransientError("rate limit 429")
            return llm_mod.LLMResponse(
                content="Selesai via fallback",
                tool_calls=[],
                model=m,
                raw_message={},
            )

        runner_mod._DEAD_MODEL_UNTIL.clear()
        try:
            with (
                mock.patch.object(runner_mod, "list_models", return_value={"ok": True, "models": ["ag/gemini-3.8-flash-medium", "ag/claude-sonnet-4-6"]}),
                mock.patch.object(runner_mod, "chat_completion", side_effect=_fake_chat),
            ):
                runner._run_loop("tes failover", attach_screen=False)

            done_ev = [e for e in events if e.get("type") == "agent_status" and e.get("status") == "idle"]
            self.assertTrue(done_ev)
            self.assertEqual(done_ev[-1].get("label"), "Selesai ✓")
            self.assertGreater(runner_mod._DEAD_MODEL_UNTIL.get("ag/gemini-3.8-flash-medium", 0.0), time.monotonic())
            self.assertTrue(any("Selesai via fallback" in str(e.get("text", "")) for e in events if e.get("type") == "assistant_message"))
        finally:
            runner_mod._DEAD_MODEL_UNTIL.clear()

    def test_swarm_clones_execution(self) -> None:
        events: list[dict] = []
        with mock.patch.object(wb, "get_monitors", return_value=[{"name": "D1", "x": 0, "y": 0, "logical_w": 1920, "logical_h": 1080, "focused": True}]):
            desktop = desktop_mod.DesktopController(on_event=events.append)

        cfg = {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test",
            "model": "google/gemini-2.5-flash",
            "max_steps": 4,
            "restore_cursor": False,
        }
        runner = runner_mod.AgentRunner(desktop=desktop, config_getter=lambda: cfg, emit=events.append)

        def _fake_clone_chat(**kwargs):
            return llm_mod.LLMResponse(
                content="Clone done",
                tool_calls=[],
                model=kwargs["model"],
                raw_message={},
            )

        with mock.patch.object(runner_mod, "chat_completion", side_effect=_fake_clone_chat):
            res = runner._run_clones(
                [
                    {"title": "Router A", "instruction": "Cek config A", "attach_screen": False},
                    {"title": "Router B", "instruction": "Cek config B", "attach_screen": False},
                ],
                cfg=cfg,
                model="google/gemini-2.5-flash",
            )
        self.assertEqual(len(res), 2)
        self.assertTrue(all(r["ok"] for r in res))
        self.assertTrue(all(r["summary"] == "Clone done" for r in res))
        self.assertTrue(any(e.get("type") == "clone_spawn" for e in events))
        self.assertTrue(any(e.get("type") == "clone_merge" for e in events))


class TestBridgeTcpRoundtrip(unittest.TestCase):
    def test_bridge_tcp_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            port_file = Path(tmp) / "bridge.port"
            events: list[dict] = []
            with (
                mock.patch.object(bridge_mod, "PORT_FILE", port_file),
                mock.patch.object(cli_mod, "PORT_FILE", port_file),
                mock.patch.object(wb, "get_monitors", return_value=[{"name": "D1", "x": 0, "y": 0, "logical_w": 1920, "logical_h": 1080, "focused": True}]),
                mock.patch.object(bridge_mod, "list_models", return_value={"ok": False, "models": [], "error": "offline"}),
            ):
                br = bridge_mod.AgentBridge(event_listener=events.append)
                port = br.start()
                try:
                    self.assertTrue(port_file.exists())
                    self.assertEqual(int(port_file.read_text(encoding="utf-8")), port)

                    st = cli_mod.send_command({"cmd": "status"})
                    self.assertTrue(st["ok"])
                    self.assertEqual(st["status"], "idle")

                    mv = cli_mod.send_command({"cmd": "move_cursor", "x": 320, "y": 240, "label": "Hi"})
                    self.assertTrue(mv["ok"])
                    self.assertEqual((mv["x"], mv["y"]), (320, 240))

                    ui_res = cli_mod.send_command({"cmd": "ui", "action": "toggle"})
                    self.assertTrue(ui_res["ok"])
                    self.assertTrue(any(e.get("type") == "ui_command" for e in events))

                    quit_res = cli_mod.send_command({"cmd": "quit"})
                    self.assertTrue(quit_res["ok"])
                finally:
                    br.shutdown()
                    self.assertFalse(port_file.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
