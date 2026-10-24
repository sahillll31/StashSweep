"""Recommendation engine.

Combines analyzer findings into prioritized suggestions. Decided rule-set:
- duplicates              -> safe, high confidence
- junk dirs (cache/build) -> safe ; node_modules is always rebuildable
- type rules (installers)-> review suggested
- large/old files         -> review suggested
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .models import Finding
from .scanner import scan as scan_files

SAFE_RULES = {"duplicates", "build_and_cache"}
REVIEW_RULES = {"junk_installers", "large_old_files"}


@dataclass
class Recommendation:
    finding: Finding
    verdict: str            # "safe" | "review"
    reason: str


def _score(finding: Finding) -> int:
    base = {"duplicates": 3, "build_and_cache": 2, "junk_installers": 1, "large_old_files": 1}
    return base.get(finding.rule, 0) * 10_000_000 + finding.reclaimable_bytes


def recommend(scan_path, cfg: Config | None = None) -> list[Recommendation]:
    """Scan + analyze + prioritize in one shot. Read-only."""
    if cfg is None:
        from .config import load_config

        cfg = load_config()

    from .analyzers import find_by_type, find_duplicates, find_junk_dirs, find_large_old

    root = scan_files(scan_path, cfg)
    findings: list[Finding] = []
    findings += find_duplicates(root)
    findings += find_large_old(root, cfg)
    findings += find_by_type(root, cfg)
    findings += find_junk_dirs(scan_path, cfg)  # needs the path, not entry list

    recs: list[Recommendation] = []
    for finding in findings:
        if finding.rule in SAFE_RULES:
            verdict = "safe"
            reason = (
                "Exact duplicate content (verified by hash) — deleting loses nothing."
                if finding.rule == "duplicates"
                else "Standard cache/build folder — recreatable at zero cost."
            )
        elif finding.rule in REVIEW_RULES:
            verdict = "review"
            reason = (
                f"Old & large — {finding.description}. Double-check before quarantining."
                if finding.rule == "large_old_files"
                else f"{finding.description}. Keep if you might still need to reinstall offline."
            )
        else:
            verdict = "review"
            reason = finding.description
        recs.append(Recommendation(finding=finding, verdict=verdict, reason=reason))

    recs.sort(key=lambda r: (-_score(r.finding), r.verdict != "safe"))
    return recs
