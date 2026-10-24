"""Chat layer.

Deterministic keyword mapping (always works, even offline).
When an Ollama server is reachable at localhost:11434 it is used to interpret
fuzzy instructions like "free up my downloads" into one of the known intents.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from rich.console import Console

console = Console()

INTENTS = [
    "scan",
    "recommend",
    "clean_safe",
    "quarantine_list",
    "quarantine_purge",
    "help",
    "exit",
]

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:0.5b"

SYSTEM_PROMPT = """You are a command parser for a disk-cleanup assistant.
Given a user message, respond with ONLY a JSON object:
{"intent": "<one of INTENTS>", "path": "<optional directory path, else null>"}
Valid intents: scan, recommend, clean_safe, quarantine_list, quarantine_purge, help, exit.
Examples:
user: what's taking up space in D:/Games -> {"intent":"scan","path":"D:/Games"}
user: clean up junk -> {"intent":"clean_safe","path":null}
user: what would you delete -> {"intent":"recommend","path":null}
user: what's in the trash -> {"intent":"quarantine_list","path":null}
user: empty the stash -> {"intent":"quarantine_purge","path":null}
user: bye -> {"intent":"exit","path":null}
Respond with JSON only, nothing else."""


def _local_llm(message: str) -> dict | None:
    """Ask a local Ollama model to classify the intent. Returns None if unavailable."""
    try:
        body = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": f"{SYSTEM_PROMPT}\nuser: {message}\nJSON:",
            "stream": False,
            "format": "json",
        }).encode("utf-8")
        req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        parsed = json.loads(data.get("response", "{}"))
        if parsed.get("intent") in INTENTS:
            return parsed
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    return None


def _keyword_intent(message: str) -> str:
    msg = message.lower()
    if any(k in msg for k in ("exit", "quit", "bye", "stop")):
        return "exit"
    if "quarantine" in msg or "trash" in msg or "stash" in msg:
        if any(k in msg for k in ("purge", "empty", "erase", "delete for good", "free the")):
            return "quarantine_purge"
        return "quarantine_list"
    if any(k in msg for k in ("clean", "clear", "delete", "junk", "free up", "remove")):
        return "clean_safe"
    if any(k in msg for k in ("recommend", "suggest", "what would you", "what can i", "advice")):
        return "recommend"
    if any(k in msg for k in ("scan", "check", "look at", "how big", "space")):
        return "scan"
    return "help"


def _extract_path(message: str) -> Path | None:
    """Find a path-looking fragment in the message."""
    import re

    for token in re.split(r"\s+", message):
        if re.match(r"^[A-Za-z]:(\\|/)", token) or token.startswith("~/"):
            try:
                return Path(token.strip(",.!?\"'"))
            except (ValueError, TypeError):
                return None
    return None


def _act(intent: str, path: Path | None) -> None:
    """Execute the intent through the normal CLI pathways (same safety gates)."""
    import subprocess, sys

    base_cmd = [sys.executable, "-m", "pa.cli"]
    if intent == "scan":
        subprocess.run(base_cmd + ["scan", str(path or Path.home())], check=False)
    elif intent == "recommend":
        subprocess.run(base_cmd + ["recommend", str(path or Path.home())], check=False)
    elif intent == "clean_safe":
        subprocess.run(base_cmd + ["recommend", "--apply", str(path or Path.home())], check=False)
    elif intent == "quarantine_list":
        subprocess.run(base_cmd + ["quarantine", "list"], check=False)
    elif intent == "quarantine_purge":
        subprocess.run(base_cmd + ["quarantine", "purge"], check=False)
    else:
        console.print(
            "[bold]I understand:[/bold]\n"
            "  • [cyan]scan <folder>[/cyan] — read-only inventory\n"
            "  • [cyan]what would you delete[/cyan] — my recommendations\n"
            "  • [cyan]clean up junk[/cyan] / [cyan]clear cache[/cyan] — quarantine the safe picks\n"
            "  • [cyan]what's in the trash[/cyan] — list quarantined items\n"
            "  • [cyan]empty the stash[/cyan] — permanently free quarantined space\n"
            "  • [cyan]exit[/cyan]"
        )


def start_chat():
    """Read-eval loop. Same engine, same safety gates — just a friendlier front."""
    console.print("[bold green]PA chat[/bold green] [dim](offline keyword mode; "
                  "install Ollama + qwen2.5:0.5b for smarter parsing)[/dim]")
    console.print("[dim]Type 'help' to see what I understand, 'exit' to leave.[/dim]\n")

    while True:
        try:
            message = console.input("[bold green]you ›[/bold green] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not message:
            continue

        llm_parse = _local_llm(message)
        if llm_parse:
            intent = llm_parse["intent"]
            raw_path = llm_parse.get("path")
            path = Path(raw_path) if raw_path else _extract_path(message)
            console.print(f"[dim](intent via Ollama: {intent})[/dim]")
        else:
            intent = _keyword_intent(message)
            path = _extract_path(message)

        if intent == "exit":
            console.print("[bold green]bye![/bold green]")
            break
        _act(intent, path)
