"""Analyzers: pure functions from a file inventory to Findings.

Guarantees:
- analyzers never touch the filesystem (read-only metadata already collected)
- each returns a list[Finding] with only entries that are candidates for cleanup
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

from ..config import Config
from ..models import FileEntry, Finding

_CHUNK = 64 * 1024


# ---------------------------------------------------------------- helpers

def _hash(path: Path, first_chunk_only: bool) -> str:
    """Hash file contents. First-chunk hash is enough to disambiguate real dupes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if first_chunk_only:
            h.update(f.read(_CHUNK))
        else:
            for chunk in iter(lambda: f.read(_CHUNK), b""):
                h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- duplicates

def find_duplicates(entries: list[FileEntry]) -> list[Finding]:
    """Group by size, then verify candidates with content hashes."""
    findable = [e for e in entries if e.size > 0]
    by_size: dict[int, list[FileEntry]] = defaultdict(list)
    for entry in findable:
        by_size[entry.size].append(entry)

    duplicates: list[tuple[FileEntry, FileEntry]] = []
    for size, group in by_size.items():
        if len(group) < 2:
            continue
        # First pass: hash head chunk of each file.
        by_head: dict[str, list[FileEntry]] = defaultdict(list)
        for entry in group:
            try:
                by_head[_hash(entry.path, first_chunk_only=True)].append(entry)
            except OSError:
                continue
        # Second pass: full hash only for the remaining head-collisions (paranoia,
        # cheap because heads rarely collide for distinct content).
        for head_group in by_head.values():
            if len(head_group) < 2:
                continue
            by_full: dict[str, list[FileEntry]] = defaultdict(list)
            for entry in head_group:
                try:
                    by_full[entry.hash or _hash(entry.path, first_chunk_only=False)].append(entry)
                except OSError:
                    continue
            for full_group in by_full.values():
                if len(full_group) < 2:
                    continue
                # Newest file is the keeper (most recently modified).
                keeper = max(full_group, key=lambda e: e.mtime)
                for entry in full_group:
                    if entry is not keeper:
                        duplicates.append((keeper, entry))

    findings: list[Finding] = []
    for keeper, dupe in duplicates:
        findings.append(
            Finding(
                rule="duplicates",
                description=f"Duplicate of {keeper.path} (kept: newest copy)",
                entries=[dupe],
                reclaimable_bytes=dupe.size,
            )
        )
    return findings


# ---------------------------------------------------------------- bloat

def find_large_old(entries: list[FileEntry], cfg: Config, *, now=None) -> list[Finding]:
    """Apply every size_rule from rules.toml."""
    from datetime import datetime

    now = now or datetime.now()
    findings: list[Finding] = []
    for rule in cfg.size_rules:
        matched = [
            e
            for e in entries
            if e.size >= rule.min_size_mb * 1024 * 1024
            and (now - e.mtime).days >= rule.max_age_days
        ]
        if matched:
            findings.append(
                Finding(
                    rule=rule.name,
                    description=rule.description,
                    entries=matched,
                    reclaimable_bytes=sum(e.size for e in matched),
                )
            )
    return findings


# ---------------------------------------------------------------- by_type

def find_by_type(entries: list[FileEntry], cfg: Config) -> list[Finding]:
    """Group installers / iso / old media per type_rules."""
    from datetime import datetime

    now = datetime.now()
    findings: list[Finding] = []
    for rule in cfg.type_rules:
        matched = [
            e
            for e in entries
            if e.path.suffix.lower() in rule.extensions
            and (now - e.mtime).days >= rule.max_age_days
        ]
        if matched:
            findings.append(
                Finding(
                    rule=rule.name,
                    description=rule.description,
                    entries=matched,
                    reclaimable_bytes=sum(e.size for e in matched),
                )
            )
    return findings


# ---------------------------------------------------------------- junk dirs

def _iter_dirs(root: Path):
    """Yield all directories under root, tolerating permission errors."""
    try:
        for p in root.rglob("*"):
            if p.is_dir() and not p.is_symlink():
                yield p
    except OSError:
        return


def _dir_size(directory: Path, skip: set[Path]) -> int:
    """Recursively total file sizes below directory, skipping `skip` subtrees."""
    total = 0
    for p in directory.rglob("*"):
        try:
            if skip and any(s == p or s in p.parents for s in skip):
                continue
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _match_dir(name: str, pattern: str) -> bool:
    """'node_modules' == exact; '*.cache' == suffix."""
    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if pattern.startswith("*."):
        return name.endswith(pattern[1:])
    return name == pattern


def find_junk_dirs(root: Path, cfg: Config) -> list[Finding]:
    """Find cleanable folders (node_modules, __pycache__, Temp, *.cache)."""
    patterns = [pat for rule in cfg.junk_dir_rules for pat in rule.patterns]
    if not patterns:
        return []

    root = Path(root).resolve()
    matched_dirs = [d for d in _iter_dirs(root) if any(_match_dir(d.name, pat) for pat in patterns)]
    # A junk dir nested inside another junk dir must not be counted twice.
    matched_dirs.sort(key=lambda d: len(d.parts))
    top_level: list[Path] = []
    absorbed: set[Path] = set()
    for d in matched_dirs:
        if any(parent in {x for x in top_level} for parent in d.parents):
            absorbed.add(d)
            continue
        top_level.append(d)

    findings: list[Finding] = []
    for rule in cfg.junk_dir_rules:
        rule_pats = rule.patterns
        for directory in top_level:
            if not any(_match_dir(directory.name, pat) for pat in rule_pats):
                continue
            size = _dir_size(directory, skip=absorbed)
            if size <= 0:
                continue
            findings.append(
                Finding(
                    rule=rule.name,
                    description=f"{rule.description}: {directory}",
                    entries=[FileEntry(path=directory, size=size, mtime=_dir_mtime(directory))],
                    reclaimable_bytes=size,
                )
            )
    return findings


def _dir_mtime(directory: Path):
    from datetime import datetime

    try:
        return datetime.fromtimestamp(directory.stat().st_mtime)
    except OSError:
        return datetime.now()
