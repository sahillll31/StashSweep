"""LLM brain for the tray agent.

Providers (configured in llm.toml or env vars):
  provider = "ollama" | "gemini" | "groq" | "openai" | "none"

The agent contract: every reply is STRICT JSON:
  {"say": "<short reply>", "tool": "<scan|recommend|old|clean|trash|restore|purge|chat>", "path": "...|null", "arg": "...|null"}

Safety is enforced OUTSIDE the LLM: tools only ever quarantine; purge confirm
is a separate UI step. The LLM proposes, the engine disposes.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

LLM_TOML = Path(__file__).resolve().parent.parent.parent / "llm.toml"

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

CONFIG: dict = {"provider": "none", "model": "", "api_key": "", "loaded": False}


def load_llm_config() -> dict:
    """lIm.toml first, env vars override. Cached after first call."""
    if CONFIG["loaded"]:
        return CONFIG
    CONFIG["loaded"] = True

    if LLM_TOML.exists():
        try:
            with open(LLM_TOML, "rb") as f:
                data = tomllib.load(f)
            llm = data.get("llm", {})
            CONFIG["provider"] = str(llm.get("provider", "none")).lower()
            CONFIG["model"] = str(llm.get("model", "") or "")
            CONFIG["api_key"] = str(llm.get("api_key", "") or "")
        except (OSError, KeyError):
            pass

    env = os.environ.get
    if env("PA_LLM_PROVIDER"):
        CONFIG["provider"] = env("PA_LLM_PROVIDER").lower()
    if env("PA_LLM_MODEL"):
        CONFIG["model"] = env("PA_LLM_MODEL")
    if env("PA_LLM_API_KEY"):
        CONFIG["api_key"] = env("PA_LLM_API_KEY")

    return CONFIG


SYSTEM_PROMPT = """You are StashSweep, a witty but careful personal disk-cleanup assistant for {user}.
You chat in one or two short lines, like Jarvis-lite.

EVERY reply must be a single JSON object, nothing else:
{{"say": "<your natural reply to the user>", "tool": "<tool or null>", "path": "<absolute folder or null>", "arg": "<extra or null>"}}

Tools:
- scan      -> size breakdown of PATH
- recommend -> what YOU would delete (safe/review verdicts)
- old       -> files not touched in a while; ARG = threshold in days (default 365)
- clean     -> stash the SAFE picks from PATH (also stash; NOT delete)
- trash     -> list quarantined items (ARG = substring to restore instead)
- restore   -> bring back item named ARG
- purge     -> user wants real deletion; still needs typed confirmation later
- chat      -> no tool needed; just talking

Facts you must never violate:
- Nothing is deleted permanently by 'clean'. Files move to a quarantine/stash folder (reversible).
- 'purge' is the only true deletion and needs the user to confirm afterwards.
- If the user asks a question you can answer directly, use tool "chat".
- If the user is saying thanks/greeting/smalltalk, use tool "chat".
"""


def _post(url: str, body: dict, headers: dict | None = None, timeout: int = 25) -> dict | None:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Some APIs (Groq behind Cloudflare) reject clients without a UA.
            "User-Agent": "StashSweep/0.1 (+https://github.com/sahillll31/StashSweep)",
            **(headers or {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    # strip markdown fence if present
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if m:
        text = m.group(1)
    dec = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = dec.raw_decode(text[start:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


def ask_llm(history: list[dict], user_msg: str, inventory: str = "") -> dict | None:
    """Returns parsed {say, tool, path, arg} or None (unreachable / bad parse)."""
    cfg = load_llm_config()
    provider = cfg["provider"]
    if provider == "none":
        return None

    system = SYSTEM_PROMPT
    last_context = ""
    if inventory:
        last_context = f"\nRecent disk-activity context:\n{inventory}\n"

    convo = [
        *[{"role": h["role"], "content": h["content"][:600]} for h in history[-8:]],
        {"role": "user", "content": f"{last_context}user: {user_msg}\nJSON:"},
    ]

    if provider == "ollama":
        model = cfg["model"] or "qwen2.5:3b"
        data = _post(
            "http://localhost:11434/api/chat",
            {"model": model, "messages": [{"role": "system", "content": system}, *convo], "stream": False},
            timeout=60,
        )
        if not data:
            return None
        raw = data.get("message", {}).get("content", "")
        return _extract_json(raw)

    if provider == "gemini":
        model = cfg["model"] or "gemini-2.0-flash"
        key = cfg["api_key"]
        if not key:
            return None
        prompt = system + "\n\n" + "\n".join(
            f"{h['role']}: {h['content']}" for h in convo
        )
        data = _post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            {"contents": [{"parts": [{"text": prompt}]}]},
        )
        if not data:
            return None
        try:
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            return _extract_json(raw)
        except (KeyError, IndexError, TypeError):
            return None

    if provider in ("groq", "openai", "openrouter"):
        key = cfg["api_key"]
        if not key:
            return None
        bases = {
            "groq": "https://api.groq.com/openai/v1",
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }
        models = {
            "groq": "llama-3.3-70b-versatile",
            "openai": "gpt-4o-mini",
            "openrouter": "openai/gpt-4o-mini",
        }
        data = _post(
            f"{bases[provider]}/chat/completions",
            {
                "model": cfg["model"] or models[provider],
                "messages": [{"role": "system", "content": system}, *convo],
                "temperature": 0.3,
            },
            {"Authorization": f"Bearer {key}"},
        )
        if not data:
            return None
        try:
            raw = data["choices"][0]["message"]["content"]
            return _extract_json(raw)
        except (KeyError, IndexError, TypeError):
            return None

    return None
