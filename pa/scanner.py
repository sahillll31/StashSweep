"""Read-only filesystem scanner.

Walks a root directory, skips denied paths, and produces FileEntry records.
Nothing in this module writes to disk.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config
from .models import FileEntry


def classify(path: Path) -> str:
    ext = path.suffix.lower()
    media = {".mp4", ".mkv", ".mov", ".avi", ".mp3", ".wav", ".jpg", ".jpeg", ".png", ".gif"}
    docs = {".pdf", ".doc", ".docx", ".txt", ".md", ".xls", ".xlsx", ".ppt", ".pptx"}
    archives = {".zip", ".rar", ".7z", ".tar", ".gz"}
    installers = {".exe", ".msi", ".dmg", ".iso", ".pkg"}
    if ext in media:
        return "media"
    if ext in docs:
        return "documents"
    if ext in archives:
        return "archives"
    if ext in installers:
        return "installers"
    return "other"


def scan(root: Path | str, cfg: Config | None = None) -> list[FileEntry]:
    """Return a flat list of FileEntry for every regular file under root."""
    root = Path(root).resolve()
    if cfg is None:
        from .config import load_config

        cfg = load_config()

    entries: list[FileEntry] = []
    try:
        for p in root.rglob("*"):
            try:
                if not p.is_file() or p.is_symlink():
                    continue
                if cfg.is_denied(p):
                    continue
                stat = p.stat()
                entries.append(
                    FileEntry(
                        path=p,
                        size=stat.st_size,
                        mtime=datetime.fromtimestamp(stat.st_mtime),
                        category=classify(p),
                    )
                )
            except OSError:
                # PermissionError, missing file mid-walk, etc. — skip safely.
                continue
    except OSError:
        pass
    return entries
