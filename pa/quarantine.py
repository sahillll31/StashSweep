"""Quarantine: reversible deletes.

Files are *moved* into a dedicated folder (never immediately destroyed).
A manifest.json tracks original locations so `pa quarantine restore` can undo moves.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST = "manifest.json"


def quarantine_root(base: Path | None = None) -> Path:
    """Where quarantined items live. Defaults to <project root>/.pa-quarantine."""
    root = base or Path(__file__).resolve().parent.parent / ".pa-quarantine"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _manifest_path(base: Path | None = None) -> Path:
    return quarantine_root(base) / MANIFEST


def _load(base: Path | None = None) -> list[dict[str, Any]]:
    path = _manifest_path(base)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: list[dict[str, Any]], base: Path | None = None) -> None:
    _manifest_path(base).write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def quarantine_item(
    source: Path, base: Path | None = None, *, batch: str | None = None
) -> dict[str, Any]:
    """Move one file or directory into quarantine. Returns its manifest record."""
    source = Path(source)
    brand = batch or datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_dir = quarantine_root(base) / brand
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest = dest_dir / source.name
    if dest.exists():
        dest = dest_dir / f"{datetime.now().strftime('%H%M%S%f')}_{source.name}"
    shutil.move(str(source), str(dest))

    size = 0
    if dest.is_dir():
        for p in dest.rglob("*"):
            if p.is_file():
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
    else:
        size = dest.stat().st_size

    record = {
        "id": f"{brand}:{source.name}",
        "original": str(source.resolve()),
        "quarantined": str(dest.resolve()),
        "size": size,
        "when": datetime.now().isoformat(timespec="seconds"),
    }
    records = _load(base)
    records.append(record)
    _save(records, base)
    return record


def list_quarantined(base: Path | None = None) -> list[dict[str, Any]]:
    return _load(base)


def restore(record_id: str, base: Path | None = None) -> bool:
    """Move one quarantined item back to its original location."""
    records = _load(base)
    for record in records:
        if record["id"] == record_id:
            src = Path(record["quarantined"])
            dst = Path(record["original"])
            if not src.exists():
                records.remove(record)
                _save(records, base)
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():  # name collision — don't clobber
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                dst = dst.with_name(f"{dst.stem}_restored_{stamp}{dst.suffix}")
            shutil.move(str(src), str(dst))
            records.remove(record)
            _save(records, base)
            return True
    return False


def purge(record_id: str, base: Path | None = None) -> int:
    """Permanently delete one quarantined item. Returns bytes freed."""
    records = _load(base)
    for record in records:
        if record["id"] == record_id:
            target = Path(record["quarantined"])
            freed = record.get("size", 0)
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            # Clean up the batch folder if it is now empty.
            if target.parent != quarantine_root(base):
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
            records.remove(record)
            _save(records, base)
            return freed
    return 0


def purge_all(base: Path | None = None) -> int:
    """Permanently delete everything currently in quarantine."""
    freed = 0
    for record in list(_load(base)):
        freed += purge(record["id"], base)
    root = quarantine_root(base)
    for batch_dir in root.iterdir():
        if batch_dir.is_dir():
            try:
                batch_dir.rmdir()
            except OSError:
                pass
    return freed
