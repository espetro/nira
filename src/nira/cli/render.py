"""Human-readable rendering of plans via rich."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from nira.core.model import Plan

_STATUS_MARKS = {
    "missing": ("INSTALL", "yellow"),
    "drifted": ("DRIFT", "red"),
    "ok": ("OK", "green"),
    "protected": ("PROT", "magenta"),
    "skipped": ("SKIP", "dim"),
}


def render_plan(plan: Plan, console: Console | None = None) -> None:
    """Render a Plan as a rich table (colors by status)."""
    console = console or Console()
    table = Table(title=f"plan: {plan.host}", show_lines=False)
    table.add_column("op", style="bold", width=7)
    table.add_column("bundle", style="cyan")
    table.add_column("component", style="white")
    table.add_column("status", width=7)
    table.add_column("detail", style="dim")

    for entry in plan.entries:
        mark, color = _STATUS_MARKS.get(str(entry.status), ("?", "white"))
        table.add_row(
            str(entry.op),
            entry.bundle,
            entry.component,
            f"[{color}]{mark}[/{color}]",
            entry.detail,
        )
    console.print(table)

    changes = len(plan.changes)
    style = "green" if changes == 0 else "yellow"
    console.print(f"[{style}]{changes} change(s)[/{style}]")


def render_results(results: list[dict], console: Console | None = None) -> None:
    """Render apply results rows."""
    console = console or Console()
    table = Table(title="apply results")
    table.add_column("bundle", style="cyan")
    table.add_column("component")
    table.add_column("result")
    table.add_column("detail", style="dim")
    for r in results:
        ok = str(r.get("result", "")).lower() in ("ok", "success", "applied", "skipped")
        table.add_row(
            r.get("bundle", ""),
            r.get("component", ""),
            f"[green]{r.get('result', '')}[/]" if ok else f"[red]{r.get('result', '')}[/]",
            r.get("detail", ""),
        )
    console.print(table)
