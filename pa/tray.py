"""StashSweep popup assistant.

Lives in the system tray. Press Ctrl+Alt+P anywhere (or click the tray icon)
and a chat window pops up: "Wassup - what are we cleaning?"

Same engine, same safety gates as the CLI:
- scan / recommend are read-only
- cleaning quarantines (reversible), never true-deletes
- purge requires typing "confirm purge"
"""

from __future__ import annotations

import io
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    from rich.console import Console
except ImportError:  # rich is a hard dep anyway, but keep tray resilient
    Console = None

from .chat import _keyword_intent
from .chat.llm import ask_llm, load_llm_config
from .config import load_config
from .models import Report
from .quarantine import list_quarantined, purge_all
from .recommend import recommend as run_recommend

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = None
    ImageDraw = None

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    from pynput import keyboard
except ImportError:
    keyboard = None

try:
    import keyboard as kbd_global
except ImportError:
    kbd_global = None

LOG_PATH = Path(__file__).resolve().parent.parent / "stash.log"


def _elog(msg: str):
    """Never fail silently again — everything the tray does gets logged."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass

try:
    import pystray
except ImportError:
    pystray = None

MB = 1024 * 1024


# --------------------------------------------------------------- helpers

def _fmt(size: int) -> str:
    if size >= MB:
        return f"{size / MB:.2f} MB"
    return f"{size / 1024:.1f} KB"


def _friendly_path(msg: str, cfg) -> Path:
    """'scan downloads/desktop/documents' -> real folder under home."""
    low = msg.lower()
    home = Path.home()
    words = {
        "downloads": home / "Downloads",
        "download": home / "Downloads",
        "desktop": home / "Desktop",
        "documents": home / "Documents",
        "pics": home / "Pictures",
        "pictures": home / "Pictures",
        "videos": home / "Videos",
        "music": home / "Music",
        "projects": home / "personal-assistant",
    }
    for word, target in words.items():
        if word in low and target.exists():
            return target
    if "home" in low or "everything" in low or "all" in low:
        return home
    # Absolute path pasted in? use it.
    for token in msg.split():
        if (":" in token and len(token) > 2) or token.startswith("~"):
            p = Path(token.strip("\"',.?"))
            if p.exists():
                return p
    return home / "Downloads"


def _quick_summary(root: Path, cfg) -> list[str]:
    """Two-line status: 'is anything reclaimable right now?'"""
    try:
        recs = run_recommend(root, cfg)
    except Exception:
        return [f"Couldn't scan {root} - folder may be missing."]
    name = root.name or str(root)
    if not recs:
        return [f"Quick check: {name} is tidy.", "Say 'scan <folder>' to check somewhere else."]
    safe = [r for r in recs if r.verdict == "safe"]
    safe_mb = sum(r.finding.reclaimable_mb for r in safe)
    total_mb = sum(r.finding.reclaimable_mb for r in recs)
    top = sorted(recs, key=lambda r: -r.finding.reclaimable_bytes)[:2]
    top_txt = ", ".join(r.finding.rule for r in top) or "no big chunks"
    if safe and safe_mb:
        return [
            f"Quick check: {total_mb:.2f} MB reclaimable in {name} ({top_txt}).",
            f"{safe_mb:.2f} MB of it is safe to stash - just say 'clean it'.",
        ]
    return [
        f"Quick check: {total_mb:.2f} MB reclaimable in {name} ({top_txt}).",
        "Nothing auto-safe - say 'what would you delete' to see why.",
    ]


def _report_text(path: Path, cfg) -> list[str]:
    recs = run_recommend(path, cfg)
    if not recs:
        return [f"Nothing worth cleaning in {path.name or path} — looks tidy."]
    lines = [f"{len(recs)} finding(s) in {path.name or path}: ", ""]
    for i, r in enumerate(recs, 1):
        verdict = "SAFE" if r.verdict == "safe" else "REVIEW"
        lines.append(f"{i}. [{verdict}] {r.finding.rule} - {_fmt(r.finding.reclaimable_bytes)}")
        lines.append(f"    {r.finding.description}")
        for e in r.finding.entries[:3]:
            lines.append(f"      - {e.path.name} ({_fmt(e.size)})")
        extra = len(r.finding.entries) - 3
        if extra > 0:
            lines.append(f"      ... and {extra} more")
    lines.append("")
    safe = sum(len(r.finding.entries) for r in recs if r.verdict == "safe")
    safe_mb = sum(r.finding.reclaimable_mb for r in recs if r.verdict == "safe")
    if safe:
        lines.append(f"Safe picks: {safe} item(s), {safe_mb:.2f} MB. Say 'clean it' to stash them.")
    lines.append("REVIEW items need your go-ahead: 'clean <rule name>'")
    return lines


# --------------------------------------------------------------- actions

class Actions:
    """GUI-side intent handlers returning list[str] lines."""

    def __init__(self):
        self.cfg = load_config()
        self.pending_purge = False
        self.last_safe_root = Path.home() / "Downloads"
        self.last_stashed: list[tuple[str, int]] = []   # (name, size) from most recent clean
        self.turns = 0
        self._last_old: list = []

    def _trash_summary(self) -> list[str]:
        items = list_quarantined()
        if not items:
            return ["Quarantine is empty - nothing pending."]
        total = sum(it["size"] for it in items)
        lines = [f"{len(items)} item(s) in the stash, {_fmt(total)} recoverable:"]
        for it in items[:10]:
            lines.append(f"   {Path(it['original']).name}  ({_fmt(it['size'])})")
        return lines

    def route(self, message: str, history: list[dict]) -> list[str]:
        """Agent path: LLM decides, deterministic engine executes, keyword fallback."""
        cfg = load_llm_config()
        if cfg["provider"] != "none":
            inventory = ""
            if self.last_stashed:
                names = ", ".join(n for n, _ in self.last_stashed[:5])
                inventory = f"Session note: stashed this session -> {names}."
            parsed = ask_llm(history, message, inventory)
            if parsed:
                say = (parsed.get("say") or "").strip()
                tool = parsed.get("tool")
                result = self._run_tool(tool, parsed)
                if result:
                    return ([say] if say else []) + [""] + result
                if say:
                    return [say, ""]
                # fall through to keyword mode
        return self.handle(message)

    def _run_tool(self, tool: str, parsed: dict) -> list[str] | None:
        """Execute the LLM's chosen tool THROUGH the deterministic engine."""
        try:
            def with_path(base: str) -> str:
                p = parsed.get("path")
                if p and (":" in str(p) or str(p).startswith("~")):
                    return f"{base} {p}"
                return base

            if tool == "scan":
                return self.handle(with_path("scan"))
            if tool == "recommend":
                return self.handle(with_path("what would you delete"))
            if tool == "old":
                arg = parsed.get("arg") or 365
                msg = f"show old files not opened for {arg} scale"
                return self.handle(with_path(msg))
            if tool == "clean":
                arg = str(parsed.get("arg") or "safe").lower()
                base = "clean old" if arg.startswith(("old", "junk")) else "clean it"
                return self.handle(with_path(base))
            if tool == "trash":
                return self.handle("what's in the trash")
            if tool == "restore":
                return self.handle(f"restore {parsed.get('arg') or ''}")
            if tool == "purge":
                return self.handle("purge")
            return None
        except Exception as exc:
            return [f"That hit a snag: {exc}"]

    def handle(self, message: str) -> list[str]:
        low = message.lower().strip()
        self.turns += 1

        # purge confirmation gate
        if self.pending_purge:
            self.pending_purge = False
            if "confirm" in low or "yes" in low or "do it" in low or "sure" in low:
                freed = purge_all()
                self.last_stashed = []
                return [
                    f"Gone for real this time. Freed {_fmt(freed)} of actual disk space.",
                    "Open question 'are they deleted' next time = truly yes.",
                ]
            return ["Purge cancelled - stash untouched."]

        # yes / no / ok  -> stay conversational, never dead-end
        if low in ("yes", "yeah", "yep", "y", "ok", "okay", "sure", "do it", "go ahead"):
            if self.last_stashed:
                names = ", ".join(n for n, _ in self.last_stashed[:5])
                return [
                    f"Already stashed: {names}.",
                    "Recoverable any time with 'restore <name>'.",
                    "Space is only truly freed after: purge -> confirm purge.",
                ]
            return ["Just say the word: 'scan downloads' or 'what would you delete'."]

        if any(k in low for k in ("thanks", "thank you", "nice", "cool", "awesome", "good job", "great")):
            return ["Anytime! Ping me when the drive feels heavy again."]

        # questions about the delete state - answer like a human, not a parser
        if any(k in low for k in (
                "are they deleted", "did you delete", "deleted?", "is it deleted",
                "where did", "what happened", "actually deleted", "really deleted", "did it work")):
            if self.pending_purge:
                self.pending_purge = False
                return ["Purge was waiting for your confirm - cancelled since you asked a question instead."]
            if self.last_stashed:
                lines = [
                    "Nope - not deleted. They're MOVED to the stash folder, fully undo-able.",
                ]
                lines += self._trash_summary()
                lines += [
                    "",
                    "Want them back? 'restore <name>'.",
                    "Want the space back for real? Type 'purge' (then 'confirm purge').",
                ]
                return lines
            items = list_quarantined()
            if items:
                lines = ["Nothing new stashed in this session, but older items are still waiting:"]
                lines += self._trash_summary()
                return lines
            return ["Nothing is sitting in quarantine right now, and nothing was stashed this session."]

        if low in ("status", "report", "how we doing", "how are we doing", "quick check", "check now"):
            if low == "quick check" or low == "check now":
                return _quick_summary(self.last_safe_root, self.cfg)
            lines = [f"Session so far: {self.turns} messages."]
            lines += self._trash_summary()
            total = sum(s for _, s in self.last_stashed)
            if total:
                lines.append(f"Stashed this session: {_fmt(total)} (pending your purge).")
            return lines


        # purge confirmation gate
        if self.pending_purge:
            self.pending_purge = False
            if "confirm" in low or "yes" in low:
                freed = purge_all()
                return [f"Gone. Freed {_fmt(freed)} of real disk space."]
            return ["Purge cancelled - quarantine untouched."]

        if low in ("hi", "hello", "hey", "yo", "wassup", "sup"):
            return ["Yo! Want me to scan something? Try: 'scan downloads' or 'what would you delete'"]

        # 'old files / not opened in a while' — understood even in keyword mode
        if any(k in low for k in ("old file", "old and", "not open", "haven't open", "not been open", "unused", "stale")):
            import re
            from datetime import datetime, timedelta
            from .scanner import scan as scan_files

            root = _friendly_path(message, self.cfg)
            days = 365
            m = re.search(r"(\d+)\s*(day|week|month|year)", low)
            if m:
                n = int(m.group(1))
                mult = {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)]
                days = n * mult
            now = datetime.now()
            cutoff = now - timedelta(days=days)

            entries = [e for e in scan_files(root, self.cfg) if e.mtime < cutoff and e.size > 5 * 1024]
            if not entries:
                return [f"Nothing older than {days} days found in {root.name or root}."]

            total = sum(e.size for e in entries)
            lines = [
                f"{len(entries)} file(s) in {root.name or root} untouched for more than {days} days "
                f"({_fmt(total)} total). Oldest stuff first:",
            ]
            for e in sorted(entries, key=lambda x: x.mtime)[:12]:
                lines.append(f"   [{e.mtime:%Y-%m-%d}] {e.path.name}  ({_fmt(e.size)})")
            extra = len(entries) - 12
            if extra > 0:
                lines.append(f"   ... and {extra} more")
            lines += ["", "Say 'clean old' to stash these (still reversible), or purge later for real deletion."]
            self._last_old = entries
            return lines

        if low.startswith("clean old"):
            entries = getattr(self, "_last_old", [])
            if not entries:
                return ["Nothing old was listed yet — say 'show old files' first."]
            from .quarantine import quarantine_item

            stashed = []
            stashed_pairs: list[tuple[str, int]] = []
            for e in sorted(entries, key=lambda x: x.mtime):
                try:
                    quarantine_item(e.path)
                    stashed.append(e.path.name)
                    stashed_pairs.append((e.path.name, e.size))
                except OSError:
                    pass
            self.last_stashed = stashed_pairs
            return [
                f"Stashed {len(stashed)} old file(s): {', '.join(stashed[:8])}.",
                "Reversible from the stash. Real deletion needs 'purge' + 'confirm purge'.",
            ]

        if low == "confirm purge":
            freed = purge_all()
            return [f"Gone. Freed {_fmt(freed)} of real disk space."]

        intent = _keyword_intent(message)

        if intent == "exit":
            return ["See you. (Close the window, I stay in the tray.)"]

        if intent == "help":
            return [
                "Try one of:",
                "   scan downloads  /  scan D:/Games      read-only report",
                "   what would you delete                  my recommendations",
                "   clean it        clean duplicates       quarantine the safe picks",
                "   what's in the trash                    list quarantined items",
                "   empty the stash / purge                free real space (needs 'confirm purge')",
                "   help                                   this list",
            ]

        if intent == "quarantine_list":
            items = list_quarantined()
            if not items:
                return ["Quarantine is empty - nothing pending."]
            lines = [f"{len(items)} item(s) stashed:"]
            for it in items[:15]:
                lines.append(f"   {Path(it['original']).name}  ({_fmt(it['size'])})")
            lines.append("Say 'restore <name>' to undo, or 'purge' to free the space for good.")
            return lines

        if low.startswith("restore"):
            want = low.replace("restore", "").strip()
            for it in list_quarantined():
                name = Path(it["original"]).name.lower()
                if want and want in name:
                    from .quarantine import restore

                    ok = restore(it["id"])
                    return [f"Restored {name}." if ok else f"Couldn't restore {name}."]
            return [f"No quarantined item matches '{want}'."]

        if intent == "quarantine_purge" or "purge" in low:
            items = list_quarantined()
            if not items:
                return ["Nothing to purge - quarantine is empty."]
            total = sum(it["size"] for it in items)
            self.pending_purge = True
            return [
                f"About to permanently erase {len(items)} item(s) = {_fmt(total)}.",
                "This cannot be undone. Type 'confirm purge' to go ahead.",
            ]

        # clean intents ------------------------------------------------
        root = _friendly_path(message, self.cfg)
        self.last_safe_root = root

        if intent == "clean_safe" or low.startswith("clean"):
            recs = run_recommend(root, self.cfg)
            safe = [r for r in recs if r.verdict == "safe"]
            # allow cleaning a specific review rule: 'clean junk_installers'
            for word in low.split():
                matched = [r for r in recs if r.finding.rule == word]
                if matched and not safe:
                    safe = matched
                    break
            if not safe:
                if self.last_stashed:
                    names = ", ".join(n for n, _ in self.last_stashed[:5])
                    return [
                        f"Nothing new matched - I already stashed {names} from there a second ago.",
                        "Say 'what's in the trash' to see the stash, or 'purge' when you're sure.",
                    ]
                return [
                    f"Nothing safe matched in {root.name or root} - it looks clean already.",
                    "Try 'what would you delete' for the full breakdown.",
                ]

            from .executor import build_plan
            from .quarantine import quarantine_item

            plan = build_plan([r.finding for r in safe], self.cfg)
            moved = 0
            stashed_names: list[tuple[str, int]] = []
            for entry, _ in plan.actions:
                try:
                    quarantine_item(entry.path)
                    moved += 1
                    stashed_names.append((entry.path.name, entry.size))
                except OSError:
                    pass
            self.last_stashed = stashed_names
            names = ", ".join(n for n, _ in stashed_names[:6])
            return [
                f"Stashed {moved} item(s) ({plan.total_mb:.2f} MB): {names}.",
                "IMPORTANT: nothing is deleted - it's all reversible from the stash.",
                "Get it back: 'restore <name>' / see it: 'what's in the trash'.",
                "When you're SURE: purge (then 'confirm purge') actually frees the space.",
            ]

        if intent == "recommend" or "would you" in low or "recommend" in low:
            return _report_text(root, self.cfg)

        if intent == "scan":
            from .scanner import scan as scan_files

            entries = scan_files(root, self.cfg)
            report = Report(root=root)
            report.scanned_files = len(entries)
            report.scanned_bytes = sum(e.size for e in entries)

            by_cat: dict[str, int] = {}
            for e in entries:
                by_cat[e.category] = by_cat.get(e.category, 0) + e.size
            top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:5]

            lines = [
                f"Scanned {root}: {len(entries)} files, {_fmt(report.scanned_bytes)}.",
                "Biggest categories:",
            ]
            for cat, size in top:
                lines.append(f"   {cat:<12} {_fmt(size)}")
            lines.append("")
            lines.append("Say 'what would you delete' to see what's reclaimable.")
            return lines

        return ["Didn't catch that. Say 'help' for what I understand."]


# --------------------------------------------------------------- GUI

DARK_BG = "#0d1117"
DARK_FG = "#e6edf3"
ACCENT = "#3fb950"


class ChatWindow:
    def __init__(self, open_reason: str = "hotkey"):
        import tkinter as tk
        from tkinter import scrolledtext

        self.tk = tk
        self.actions = Actions()
        self.win = tk.Tk()
        self.win.title("StashSweep")
        self.win.configure(bg=DARK_BG)
        self.win.geometry("560x540")
        self.win.minsize(420, 380)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)

        header = tk.Label(
            self.win,
            text="StashSweep",
            font=("Segoe UI", 13, "bold"),
            bg=DARK_BG,
            fg=ACCENT,
            anchor="w",
        )
        header.pack(fill="x", padx=14, pady=(12, 4))

        self.log = scrolledtext.ScrolledText(
            self.win, bg=DARK_BG, fg=DARK_FG, font=("Consolas", 10),
            borderwidth=0, insertbackground=DARK_FG, state="disabled", wrap="word",
        )
        self.log.pack(fill="both", expand=True, padx=14)

        row = tk.Frame(self.win, bg=DARK_BG)
        row.pack(fill="x", padx=14, pady=10)
        self.entry = tk.Entry(
            row, bg="#161b22", fg=DARK_FG, font=("Segoe UI", 11),
            insertbackground=DARK_FG, borderwidth=0,
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=7, padx=(0, 8))
        self.entry.bind("<Return>", self.send)
        send_btn = tk.Button(
            row, text="Send", command=self.send,
            bg="#238636", fg="white", activebackground="#2ea043",
            activeforeground="white", borderwidth=0, font=("Segoe UI", 10, "bold"),
            padx=14,
        )
        send_btn.pack(side="right")

        self.entry.focus_set()
        self.history: list[dict] = []

        greeting = ["Wassup"]
        self._post(greeting, tag="bot")

        self._speak("Wassup")

        if keyboard is not None:
            self._hotkey_listener = None
        self.win.attributes("-topmost", True)
        self.win.after(250, lambda: self.win.attributes("-topmost", False))

    # ---- internals

    def _speak(self, text: str):
        if pyttsx3 is None:
            return

        def job():
            try:
                engine = pyttsx3.init()
                engine.setProperty("rate", 175)
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass

        threading.Thread(target=job, daemon=True).start()

    def _post(self, lines, tag="bot"):
        self.log.configure(state="normal")
        stamp = datetime.now().strftime("%H:%M")
        prefix = {"bot": "stashsweep", "you": "you"}.get(tag, tag)
        self.log.insert("end", f"[{prefix} {stamp}]\n", tag)
        for line in lines:
            self.log.insert("end", line + "\n", tag)
        self.log.insert("end", "\n")
        if tag == "bot":
            self.log.tag_configure("bot", foreground=ACCENT)
        self.log.tag_configure("you", foreground="#79c0ff")
        self.log.configure(state="disabled")
        self.log.see("end")
        role = "assistant" if tag != "you" else "user"
        self.history.append({"role": role, "content": "\n".join(str(l) for l in lines)})

    def send(self, event=None):
        msg = self.entry.get().strip()
        if not msg:
            return
        self.entry.delete(0, "end")
        self._post([msg], tag="you")
        threading.Thread(target=self._respond, args=(msg,), daemon=True).start()

    def _respond(self, msg: str):
        try:
            lines = self.actions.route(msg, self.history)
        except Exception as exc:
            lines = [f"That went sideways: {exc}"]
        self.win.after(0, lambda: self._post(lines, tag="bot"))

    def hide(self):
        self.win.withdraw()

    def run(self):
        self.win.mainloop()

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.entry.focus_set()

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


# --------------------------------------------------------------- tray

def _icon_image():
    if Image is None:
        return None
    img = Image.new("RGB", (64, 64), "#0d1117")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=14, fill="#238636")
    d.ellipse([16, 24, 30, 38], fill="#e6edf3")
    d.ellipse([32, 24, 46, 38], fill="#79c0ff")
    d.rectangle([18, 46, 46, 50], fill="#0d1117")
    return img


def run_tray():
    def on_open(icon, item):
        open_window("tray")

    def on_quit(icon, item):
        win = state["window"]
        if win is not None:
            try:
                win.win.after(0, win.destroy)
            except Exception:
                pass
        icon.stop()

    if pystray is None or Image is None:
        # fallback: just wait, no tray icon
        while True:
            time.sleep(999)
    else:
        menu = pystray.Menu(
            pystray.MenuItem("Open chat (Ctrl+Alt+P)", on_open, default=True),
            pystray.MenuItem("Quit", on_quit),
        )
        icon = pystray.Icon("StashSweep", _icon_image(), "StashSweep", menu)
        icon.run()


def open_window(reason: str = "tray"):
    """Open (or focus) the chat window. Safe to call from any thread."""
    def job():
        win = state["window"]
        if win is not None:
            try:
                if win.win.winfo_exists():
                    win.show()
                    return
            except Exception:
                pass
        try:
            win = ChatWindow(open_reason=reason)
            state["window"] = win
            win.run()
        finally:
            state["window"] = None

    threading.Thread(target=job, daemon=True).start()


state = {"window": None}


def run_hotkey():
    """Ctrl+Alt+P -> popup. Uses the `keyboard` lib first (most reliable on
    Windows), falls back to pynput's GlobalHotKeys."""
    _elog("hotkey: registering")

    def on_hot():
        _elog("hotkey: FIRED")
        try:
            open_window("hotkey")
        except Exception as exc:
            _elog(f"hotkey: open_window failed: {exc!r}")

    registered = False
    if kbd_global is not None:
        try:
            kbd_global.add_hotkey("ctrl+alt+p", on_hot)
            registered = True
            _elog("hotkey: registered via `keyboard` lib")
        except Exception as exc:
            _elog(f"hotkey: keyboard-lib failed: {exc!r}")
    if not registered and keyboard is not None:
        threading.Thread(
            target=_pynput_hotkey_loop, args=(on_hot,), daemon=True
        ).start()
        registered = True
        _elog("hotkey: registered via pynput fallback")
    if not registered:
        _elog("hotkey: NO mechanism available!")
    if registered:
        threading.Thread(target=_hotkey_keepalive, daemon=True).start()


def _hotkey_keepalive():
    while True:
        time.sleep(3600)


def _pynput_hotkey_loop(on_hot):
    def on_hot_pynput():
        _elog("hotkey: FIRED (pynput)")
        try:
            on_hot()
        except Exception as exc:
            _elog(f"hotkey pynput dispatch: {exc!r}")

    with keyboard.GlobalHotKeys({"<ctrl>+<alt>+p": on_hot_pynput}):
        while True:
            time.sleep(3600)


def main():
    run_hotkey()
    run_tray()


if __name__ == "__main__":
    main()
