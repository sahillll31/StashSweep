"""Configuration + deny-list loading from rules.toml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.toml"


@dataclass
class SizeRule:
    name: str
    description: str
    min_size_mb: int = 0
    max_age_days: int = 0


@dataclass
class TypeRule:
    name: str
    description: str
    extensions: list[str] = field(default_factory=list)
    max_age_days: int = 0


@dataclass
class JunkDirRule:
    name: str
    description: str
    patterns: list[str] = field(default_factory=list)


@dataclass
class Config:
    deny_paths: list[str] = field(default_factory=list)
    size_rules: list[SizeRule] = field(default_factory=list)
    type_rules: list[TypeRule] = field(default_factory=list)
    junk_dir_rules: list[JunkDirRule] = field(default_factory=list)
    quarantine_dir: str = ".pa-quarantine"

    def is_denied(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return True
        for deny in self.deny_paths:
            try:
                deny_path = Path(deny).resolve()
            except OSError:
                continue
            if resolved == deny_path or deny_path in resolved.parents:
                return True
        return False


def load_config(path: Path = RULES_PATH) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    cfg = Config()
    deny = data.get("deny_paths", {})
    cfg.deny_paths = list(deny.get("list", []))

    for r in data.get("size_rules", []):
        cfg.size_rules.append(
            SizeRule(
                name=r.get("name", ""),
                description=r.get("description", ""),
                min_size_mb=int(r.get("min_size_mb", 0)),
                max_age_days=int(r.get("max_age_days", 0)),
            )
        )

    for r in data.get("type_rules", []):
        cfg.type_rules.append(
            TypeRule(
                name=r.get("name", ""),
                description=r.get("description", ""),
                extensions=[e.lower() for e in r.get("extensions", [])],
                max_age_days=int(r.get("max_age_days", 0)),
            )
        )

    for r in data.get("junk_dir_rules", []):
        cfg.junk_dir_rules.append(
            JunkDirRule(
                name=r.get("name", ""),
                description=r.get("description", ""),
                patterns=list(r.get("patterns", [])),
            )
        )

    q = data.get("quarantine", {})
    cfg.quarantine_dir = str(q.get("dir", ".pa-quarantine"))

    return cfg
