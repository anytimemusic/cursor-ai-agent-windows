from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def get_config_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "cursor-ai-agent"
    return Path.home() / ".config" / "cursor-ai-agent"


CONFIG_DIR = get_config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "9router": {
        "label": "9router (Local AI Router)",
        "base_url": "http://localhost:20128/v1",
        "default_key": "9router",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_key": "",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_key": "",
    },
    "ollama": {
        "label": "Ollama (Local)",
        "base_url": "http://localhost:11434/v1",
        "default_key": "ollama",
    },
    "gemini": {
        "label": "Gemini (OpenAI-Compat)",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_key": "",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "9router",
    "base_url": "http://localhost:20128/v1",
    "api_key": "9router",
    "model": "",
    "vision_enabled": True,
    "som_enabled": True,
    "max_steps": 45,
    "restore_cursor": True,
    "glide_ms": 110,
    "always_show_cursor": True,
    "show_trail": True,
    "cursor_name": "Type here...",
    "cursor_accent": "#38bdf8",
    "screenshot_width": 1440,
    "hud_side": "right",
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if k in DEFAULT_CONFIG or k == "system_prompt_extra":
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    for k, v in updates.items():
        if v is not None:
            cfg[k] = v
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(CONFIG_PATH)
    return cfg
