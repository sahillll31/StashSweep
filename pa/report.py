from __future__ import annotations

import json
from dataclasses import asdict

from .models import Finding, Report
from rich.console import Console
from rich.table import Table

console = Console()


def _fmt_bytes(num: float) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} PB"


def print_report(report: Report) -> None:
    console.print(f"\n[bold]Scan report for[/bold] {report.root}")
    console.print(f"Files scanned: [cyan]{report.scanned_files}[/cyan]  "
                  f"Total size: [cyan]{_fmt_bytes(report.scanned_bytes)}[/cyan]\n")

    if not report.findings:
        console.print("[green]No findings.[/green]")
        return

    table = Table(title="Reclaimable space", show_lines=True)
    table.add_column("Rule", style="bold")
    table.add_column("Files", justify="right")
    table.add_column("Reclaimable", justify="right")
    table.add_column("Description")

    for finding in report.findings:
        table.add_row(
            finding.rule,
            str(len(finding.entries)),
            _fmt_bytes(finding.reclaimable_bytes),
            finding.description,
        )

    table.add_row(
        "[bold]TOTAL[/bold]",
        str(sum(len(f.entries) for f in report.findings)),
        f"[bold]{_fmt_bytes(report.total_reclaimable_mb * 1024 * 1024)}[/bold]",
        "",
    )
    console.print(table)
    console.print("\n[dim]Run `pa recommend` to see per-file details and priorities.[/dim]\n")


def print_findings(findings: list[Finding], limit: int = 10) -> None:
    if not findings:
        console.print("[green]Nothing matches your rules — disk looks clean.[/green]")
        return
    for finding in findings:
        console.print(f"\n[bold cyan]{finding.rule}[/bold cyan] — {finding.description} "
                      f"[dim]({_fmt_bytes(finding.reclaimable_bytes)})")
        for i, entry in enumerate(finding.entries[:limit], 1):
            console.print(f"  {i:>3}. {entry.path.name} "
                          f"[dim]({entry.path.parent}) [/dim]"
                          f"[cyan]{_fmt_bytes(entry.size)}[/cyan] "
                          f"[dim]{entry.path.stat().st_mtime if False else entry.mtime:%Y-%m-%d}[/dim]")
        if len(finding.entries) > limit:
            console.print(f"  … and {len(finding.entries) - limit} more")


def report_to_dict(report: Report) -> dict:
    def entry(e):
        return {
            "path": str(e.path),
            "size": e.size,
            "mtime": e.mtime.isoformat(),
            "category": e.category,
        }

    def finding(f: Finding):
        return {
            "rule": f.rule,
            "description": f.description,
            "count": len(f.entries),
            "reclaimable_bytes": f.reclaimable_bytes,
            "entries": [entry(e) for e in f.entries],
        }

    return {
        "root": str(report.root),
        "scanned_files": report.scanned_files,
        "scanned_bytes": report.scanned_bytes,
        "total_reclaimable_bytes": int(report.total_reclaimable_mb * 1024 * 1024),
        "findings": [finding(f) for f in report.findings],
    }


def print_json(obj) -> None:
    import sys

    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")
