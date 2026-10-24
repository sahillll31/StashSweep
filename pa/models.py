"""Core data models for scan findings and actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass
class FileEntry:
    path: Path
    size: int
    mtime: datetime
    category: str = "other"
    hash: str | None = None


@dataclass
class Finding:
    rule: str
    description: str
    entries: list[FileEntry]
    reclaimable_bytes: int = 0

    @property
    def reclaimable_mb(self) -> float:
        return self.reclaimable_bytes / (1024 * 1024)


@dataclass
class Action:
    kind: Literal["delete", "quarantine", "skip"]
    entry: FileEntry
    reason: str


@dataclass
class Report:
    root: Path
    scanned_files: int = 0
    scanned_bytes: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def total_reclaimable_mb(self) -> float:
        return sum(f.reclaimable_bytes for f in self.findings) / (1024 * 1024)
