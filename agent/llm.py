from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class RequestCancelledError(RuntimeError):
    """Raised immediately when the user stops/cancels an in-flight LLM request."""


class LLMTransientError(RuntimeError):
    """Raised when an LLM endpoint times out or returns 502/503/529 so runner can fail over cleanly."""


def normalize_base_url(base_url: str) -> str:
    url = (base_url or "http://localhost:20128/v1").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.path in ("", "/"):
        url = f"{url}/v1"
    return url


def _make_headers(api_key: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CursorAIAgent/1.0",
    }
    key = (api_key or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def format_connection_error(root: str, exc: BaseException) -> str:
    """Format network/connection errors into concise, provider-aware user messages."""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    low = text.lower()
    is_refused = (
        isinstance(reason, ConnectionRefusedError)
        or "10061" in low
        or "connection refused" in low
        or "actively refused" in low
    )
    if is_refused:
        if "20128" in root:
            return (
                "9router belum aktif di localhost:20128. "
                "Jalankan 9router dulu, atau ganti Provider (OpenRouter / Gemini / OpenAI)."
            )
        if "11434" in root:
            return "Ollama belum aktif di localhost:11434. Jalankan Ollama, atau ganti Provider lain."
        return f"Koneksi ke {root} ditolak. Pastikan server endpoint aktif."
    if isinstance(exc, TimeoutError) or "timed out" in low:
        return f"Timeout saat menghubungi {root}."
    return text


def list_models(base_url: str, api_key: str = "", timeout: float = 5.0) -> dict[str, Any]:
    root = normalize_base_url(base_url)
    url = f"{root}/models"
    req = urllib.request.Request(url, headers=_make_headers(api_key), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
        items = data.get("data") or data.get("models") or []
        ids: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)
        return {"ok": True, "models": ids, "base_url": root, "error": ""}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        if e.code in (401, 403):
            return {"ok": False, "models": [], "base_url": root, "error": f"API Key ditolak (HTTP {e.code}). Cek API Key Anda."}
        return {"ok": False, "models": [], "base_url": root, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "models": [], "base_url": root, "error": format_connection_error(root, e)}


def pick_default_model(models: list[str]) -> str:
    if not models:
        return ""
    preferred_patterns = [
        "gemini-3.8-flash",
        "gemini-3.8-flash-low",
        "gemini-2.5-flash",
        "claude-sonnet",
        "gemini-4-carbon",
        "gemini",
        "claude",
        "gpt-4o",
        "gpt-4.1",
        "gpt-5",
        "qwen",
    ]
    for pat in preferred_patterns:
        for m in models:
            low = m.lower()
            if low == pat or low.endswith("/" + pat):
                return m
    for pat in preferred_patterns:
        for m in models:
            if pat in m.lower():
                return m
    return models[0]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall]
    model: str
    raw_message: dict[str, Any]


def _parse_json_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    s = raw.strip()
    if not s:
        return {}
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        m = re.search(r"\{[\s\S]*\}", s)
        if m:
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
    return {}


def _extract_fallback_tool_calls(content: str) -> tuple[str, list[ToolCall]]:
    """Parse JSON tool_call blocks if a model responds with inline JSON instead of native tool_calls."""
    if not content:
        return "", []
    calls: list[ToolCall] = []
    cleaned = content
    pattern = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.MULTILINE)
    matches = list(pattern.finditer(content))
    for idx, m in enumerate(matches):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(obj, dict) and ("tool" in obj or "name" in obj):
            name = str(obj.get("tool") or obj.get("name"))
            args = obj.get("arguments") or obj.get("args") or {}
            if isinstance(args, str):
                args = _parse_json_args(args)
            calls.append(ToolCall(id=f"fallback_{idx}", name=name, arguments=args if isinstance(args, dict) else {}))
            cleaned = cleaned.replace(m.group(0), "").strip()
    return cleaned, calls


def _strip_images_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    texts.append("[Screenshot omitted: selected model does not accept image inputs]")
            new_msg = dict(msg)
            new_msg["content"] = "\n".join(t for t in texts if t)
            stripped.append(new_msg)
        else:
            stripped.append(msg)
    return stripped


def _parse_sse_chat_completion(raw: str, fallback_model: str) -> dict[str, Any]:
    """Assemble OpenAI-compatible SSE `data:` chunks into a standard chat.completion response."""
    content_parts: list[str] = []
    tool_map: dict[int, dict[str, Any]] = {}
    out_model = fallback_model

    for ln in raw.splitlines():
        s = ln.strip()
        if not s.startswith("data:"):
            continue
        payload = s[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            chunk = json.loads(payload)
        except Exception:
            continue
        if chunk.get("model"):
            out_model = str(chunk["model"])
        for ch in chunk.get("choices") or []:
            delta = ch.get("delta") or ch.get("message") or {}
            txt = delta.get("content")
            if isinstance(txt, str) and txt:
                content_parts.append(txt)
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index", 0))
                slot = tool_map.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = str(tc["id"])
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    slot["function"]["arguments"] += str(fn["arguments"])

    msg: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_map:
        msg["tool_calls"] = [tool_map[k] for k in sorted(tool_map)]
    return {"model": out_model, "choices": [{"message": msg}]}


def chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    timeout: float = 50.0,
    cancel_event: threading.Event | None = None,
) -> LLMResponse:
    root = normalize_base_url(base_url)
    url = f"{root}/chat/completions"

    def _post(msgs: list[dict[str, Any]], use_tools: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs,
            "temperature": 0.2,
            "stream": False,
        }
        if use_tools and tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=_make_headers(api_key), method="POST")

        box: dict[str, Any] = {}
        done_ev = threading.Event()

        def _worker() -> None:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                if raw.lstrip().startswith("data:"):
                    box["data"] = _parse_sse_chat_completion(raw, model)
                else:
                    box["data"] = json.loads(raw)
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc
            finally:
                done_ev.set()

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        while not done_ev.wait(timeout=0.15):
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelledError("Permintaan LLM dibatalkan.")
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Permintaan LLM dibatalkan.")
        if "exc" in box:
            raise box["exc"]
        return box["data"]

    cur_msgs = messages
    use_tools = bool(tools)

    for attempt in range(2):
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("Permintaan LLM dibatalkan.")
        try:
            data = _post(cur_msgs, use_tools)
            break
        except RequestCancelledError:
            raise
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            low = err_body.lower()
            if e.code == 401:
                if "20128" in root:
                    raise RuntimeError(
                        "API Key ditolak (HTTP 401). Ambil/buat API Key di dashboard 9router (http://localhost:20128), "
                        "lalu masukkan ke Settings (⚙) Kursor AI."
                    ) from e
                raise RuntimeError(
                    "API Key ditolak (HTTP 401). Periksa Provider & API Key Anda di Settings (⚙) Kursor AI."
                ) from e
            if e.code in (502, 503, 504, 529):
                raise LLMTransientError(f"LLM API error HTTP {e.code}: {err_body[:240]}") from e
            if attempt < 1 and e.code == 429:
                if cancel_event is not None and cancel_event.wait(1.0):
                    raise RequestCancelledError("Permintaan LLM dibatalkan.")
                time.sleep(0.2)
                continue
            if attempt < 1 and e.code in (400, 422):
                if use_tools and any(k in low for k in ("tool", "function", "schema")):
                    use_tools = False
                    continue
                if any(k in low for k in ("image", "vision", "multimodal", "image_url")) and cur_msgs is messages:
                    cur_msgs = _strip_images_from_messages(messages)
                    continue
            raise RuntimeError(f"LLM API error HTTP {e.code}: {err_body[:400]}") from e
        except (TimeoutError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", str(e))
            is_timeout = isinstance(e, TimeoutError) or "timed out" in str(reason).lower()
            if is_timeout:
                raise LLMTransientError(f"Model '{model}' di {root} timeout setelah {int(timeout)}s.") from e
            raise RuntimeError(format_connection_error(root, e)) from e

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"Respons LLM kosong: {json.dumps(data)[:300]}")

    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    if isinstance(content, list):
        content = "\n".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )

    parsed_calls: list[ToolCall] = []
    for idx, tc in enumerate(msg.get("tool_calls") or []):
        fn = tc.get("function") or {}
        name = fn.get("name") or ""
        if name:
            parsed_calls.append(
                ToolCall(
                    id=str(tc.get("id") or f"call_{idx}"),
                    name=name,
                    arguments=_parse_json_args(fn.get("arguments")),
                )
            )

    if not parsed_calls and content:
        cleaned, fallback_calls = _extract_fallback_tool_calls(content)
        if fallback_calls:
            content = cleaned
            parsed_calls = fallback_calls

    return LLMResponse(
        content=content.strip(),
        tool_calls=parsed_calls,
        model=str(data.get("model") or model),
        raw_message=msg,
    )
