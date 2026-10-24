"""PA CLI. Defaults are read-only; anything destructive requires --apply."""

from __future__ import annotations
from pathlib import Path
import typer
from rich.console import Console

from .config import load_config
from .models import Report
from .report import (
    print_findings,
    print_json,
    print_report,
    report_to_dict,
)
from .scanner import scan as scan_files

app = typer.Typer(add_completion=False, help="PA — personal file assistant. Safe by default (dry-run + quarantine).")
quarantine_app = typer.Typer(add_completion=False, help="Restore or purge quarantined items.")
app.add_typer(quarantine_app, name="quarantine")

console = Console()


def _analyze(path: Path, cfg, rules: list[str] | None = None):
    from .analyzers import find_by_type, find_duplicates, find_junk_dirs, find_large_old

    entries = scan_files(path, cfg)
    findings = []
    if rules is None or "duplicates" in rules:
        findings += find_duplicates(entries)
    if rules is None or "large_old" in rules:
        findings += find_large_old(entries, cfg)
    if rules is None or "by_type" in rules:
        findings += find_by_type(entries, cfg)
    if rules is None or "junk" in rules:
        findings += find_junk_dirs(path, cfg)
    return entries, findings


@app.command()
def scan(
    path: Path = typer.Argument(Path.home(), help="Folder to scan."),
    analyze: bool = typer.Option(False, "--analyze", help="Run analyzers (duplicates, junk, etc.) after the scan."),
    rules: str = typer.Option(None, help="Comma-separated rule names to run. Default = all."),
    json_out: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
):
    """Read-only inventory. Nothing is ever deleted or moved here."""
    cfg = load_config()
    rule_list = [r.strip() for r in rules.split(",")] if rules else None
    entries, findings = _analyze(path, cfg, rule_list)

    report = Report(root=path)
    report.scanned_files = len(entries)
    report.scanned_bytes = sum(e.size for e in entries)
    report.findings = findings

    if json_out:
        print_json(report_to_dict(report))
    elif analyze:
        print_report(report)
        print_findings(findings)
    else:
        print_report(report)


@app.command()
def recommend(
    path: Path = typer.Argument(Path.home(), help="Root to scan for suggestions."),
    apply: bool = typer.Option(False, "--apply", help="Actually move the 'safe' picks to quarantine."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
    json_out: bool = typer.Option(False, "--json", help="Output machine-readable JSON."),
):
    """Ask PA what it thinks you can safely reclaim."""
    from .recommend import recommend as run_rec
    from .executor import apply as executor_apply, build_plan

    cfg = load_config()
    recs = run_rec(path, cfg)

    if not recs:
        print("Nothing worth cleaning right now.")
        return

    if json_out:
        print_json([
            {"rule": r.finding.rule, "verdict": r.verdict, "reason": r.reason,
             "reclaimable_mb": round(r.finding.reclaimable_mb, 2),
             "count": len(r.finding.entries),
             "entries": [str(e.path) for e in r.finding.entries]}
            for r in recs
        ])
        return

    print_findings([r.finding for r in recs])
    safe = [r for r in recs if r.verdict == "safe"]
    if safe and apply:
        total_mb = sum(r.finding.reclaimable_mb for r in safe)
        print(f"\nSafe picks: {sum(len(r.finding.entries) for r in safe)} files, "
              f"{total_mb:.2f} MB reclaimable.")
        plan = build_plan([r.finding for r in safe], cfg)
        executor_apply(plan, cfg, assume_yes=yes)
    elif safe:
        review = sum(len(r.finding.entries) for r in safe)
        total_mb = sum(r.finding.reclaimable_mb for r in safe)
        console.print(f"[green]{review} item(s) flagged as safe to remove ({total_mb:.2f} MB).[/green]")
        print("[dim]Run `pa recommend --apply` to move just those to quarantine.[/dim]")


@app.command()
def clean(
    path: Path = typer.Argument(Path.home(), help="Root to scan for cleanup candidates."),
    rule: str = typer.Option(None, "--rule", help="Which rule to clean. Use --list-rules to see names."),
    list_rules: bool = typer.Option(False, "--list-rules", help="Show available rule names and exit."),
    apply: bool = typer.Option(False, "--apply", help="Actually run cleanup."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation (still quarantine, still reversible)."),
):
    """Clean up by rule. Default = dry-run preview only."""
    from .recommend import recommend as run_rec
    from .executor import apply as executor_apply, build_plan

    cfg = load_config()
    known = sorted({r.finding.rule for r in run_rec(path, cfg)})
    if list_rules or not rule:
        print("Available rules:", ", ".join(known))
        print("Example: pa clean --rule build_and_cache --apply")
        return

    recs = run_rec(path, cfg)
    matched = [r for r in recs if r.finding.rule == rule and not (not apply)]
    if not matched:
        console.print(f"[yellow]No findings matched rule '{rule}'.[/yellow]")
        print(f"Available rules: {', '.join(known)}")
        return

    findings = [r.finding for r in matched]
    plan = build_plan(findings, cfg)
    if not apply:
        from .executor import preview as exec_preview

        exec_preview(plan)
        print("[dim]Dry-run mode — nothing moved. Add --apply to quarantine these.[/dim]")
        return
    executor_apply(plan, cfg, assume_yes=yes)


@quarantine_app.command("list")
def quarantine_list():
    from .quarantine import list_quarantined

    items = list_quarantined()
    if not items:
        console.print("[green]Quarantine is empty.[/green]")
        return
    print(f"Quarantined items ({len(items)}):")
    for i, item in enumerate(items, 1):
        print(f"  [{item['id']}] {Path(item['original']).name} (from {Path(item['original']).parent})")


@quarantine_app.command("restore")
def quarantine_restore(id: str = typer.Argument(None, help="Record id to restore. Omit to restore all.")):
    from .quarantine import list_quarantined, restore

    if id:
        ok = restore(id)
        console.print("[green]Restored.[/green]" if ok else f"[red]No such record: {id}[/red]")
        return
    for record in list(list_quarantined()):
        if restore(record["id"]):
            print(f"Restored {record['id']}")


@quarantine_app.command("purge")
def quarantine_purge(
    id: str = typer.Argument(None, help="Record id to purge. Omit to purge all."),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation."),
):
    """Permanently delete quarantined items. This is the only true-destroy endpoint."""
    from rich.prompt import Confirm
    from .quarantine import list_quarantined, purge, purge_all

    items = list_quarantined()
    if not items:
        console.print("[green]Nothing to purge.[/green]")
        return
    confirm = yes or Confirm.ask(
        f"Permanently erase {len(items)} quarantined item(s)? This cannot be undone.", default=False)

    if not confirm:
        print("[yellow]Cancelled.[/yellow]")
        return

    if id:
        freed = purge(id)
        console.print(f"[green]Purged {id}. Freed {freed / 1024 / 1024:.2f} MB.[/green]" if freed else f"[red]No such record: {id}[/red]")
    else:
        freed = purge_all()
        console.print(f"[green]Purged {len(items)} item(s). Freed {freed / 1024 / 1024:.2f} MB.[/green]")


@app.command()
def chat():
    """Natural-language interface (internal rule-mapper; plug-in for Ollama later)."""
    from .chat import start_chat

    start_chat()


@app.command()
def version():
    from . import __version__

    print(f"pa {__version__}")


def main():
    app()


if __name__ == "__main__":
    main()

