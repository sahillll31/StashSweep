"""Executor — the only module allowed to modify the filesystem.

Flow: build plan → dry-run preview → explicit confirm → move to quarantine.
Nothing is hard-deleted here; freeing space happens later via `pa quarantine purge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from .config import Config
from .models import FileEntry, Finding
from .quarantine import quarantine_item

console = Console()


@dataclass
class Plan:
    findings: list[Finding]
    actions: list[tuple[FileEntry, str]]  # (entry, reason)

    @property
    def total_bytes(self) -> int:
        return sum(e.size for e, _ in self.actions)

    @property
    def total_mb(self) -> float:
        return self.total_bytes / (1024 * 1024)


def build_plan(findings: list[Finding], cfg: Config) -> Plan:
    """Turn findings into an actionable plan, filtering denied paths."""
    actions: list[tuple[FileEntry, str]] = []
    for finding in findings:
        for entry in finding.entries:
            if cfg.is_denied(entry.path) or not entry.path.exists():
                continue
            actions.append((entry, finding.description))
    return Plan(findings=findings, actions=actions)


def preview(plan: Plan) -> None:
    if not plan.actions:
        console.print("[yellow]Nothing to do — nothing matched your rules.[/yellow]")
        return
    table = Table(title=f"Dry-run preview — {plan.total_mb:.2f} MB recoverable", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Target")
    table.add_column("Size", justify="right")
    table.add_column("Why")
    for i, (entry, reason) in enumerate(plan.actions[:50], 1):
        label = f"[dim]{entry.path.name}[/dim]" if len(plan.actions) > 50 else entry.path.name
        table.add_row(str(i), label, f"{entry.size / 1024 / 1024:.2f} MB", reason)
    console.print(table)
    if len(plan.actions) > 50:
            console.print("[dim]" + "... and {0} more targets ...".format(len(plan.actions) - 50) + "\n")
    console.print(
        "[green]Nothing is deleted yet.[/green] Confirm below to move these to quarantine "
        "(reversible via `pa quarantine restore`).\n"
    )


def apply(plan: Plan, cfg: Config, assume_yes: bool = False) -> int:
    """Execute plan as moves-to-quarantine. Returns count of items moved."""
    preview(plan)
    if not plan.actions:
        return 0

    if not assume_yes:
        if not Confirm.ask(
            f"[bold green]Move {len(plan.actions)} items "
            f"({plan.total_mb:.2f} MB) to quarantine?[/bold green]",
            default=True,
            console=console,
        ):
            console.print("[yellow]Cancelled — nothing moved.[/yellow]")
            return 0

    moved = 0
    with console.status("Quarantining..."):
        for entry, _ in plan.actions:
            try:
                quarantine_item(entry.path)
                moved += 1
            except (OSError, PermissionError) as exc:
                console.print(f"[yellow]Skipped {entry.path}: {exc}[/yellow]")
    console.print(
        f"[bold green]Done.[/bold green] {moved} item(s) in quarantine, {plan.total_mb:.2f} MB planned. "
        "Run `pa quarantine purge` to actually free the space."
    )
    return moved
