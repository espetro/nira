"""nira CLI: plan / apply / assess / doctor / init."""

from __future__ import annotations

import typer

app = typer.Typer(help="nira: manifest-first, idempotent fleet replicator", no_args_is_help=True)
fleet_app = typer.Typer(help="Fleet repo management")
app.add_typer(fleet_app, name="fleet")


@app.command()
def plan(
    host: str = typer.Option(..., "--host", help="Host name from inventory"),
    fleet: str = typer.Option(..., "--fleet", envvar="NIRA_FLEET", help="Path to fleet repo"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the gap between manifest and host state. Installs nothing."""
    import json
    from pathlib import Path

    from nira.core.planner import build_plan

    p = build_plan(_resolve_host(host, Path(fleet)))
    if json_output:
        typer.echo(p.to_json())
    else:
        _render_plan(p)


@app.command()
def apply(
    host: str = typer.Option(..., "--host"),
    fleet: str = typer.Option(..., "--fleet", envvar="NIRA_FLEET"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Apply only the gap from plan. Re-run must produce zero changes."""
    from pathlib import Path

    from nira.ops.apply import apply_plan

    p = build_plan(_resolve_host(host, Path(fleet)))
    results = apply_plan(p)
    if json_output:
        typer.echo(json.dumps(results, indent=2))
    else:
        for r in results:
            typer.echo(f"{r['component']}: {r['result']}")


@app.command()
def doctor(
    host: str = typer.Option(..., "--host"),
    fleet: str = typer.Option(..., "--fleet", envvar="NIRA_FLEET"),
) -> None:
    """Health checks: connectivity, tailscale, pairing state, service status."""
    from pathlib import Path

    from nira.ops.doctor import run_checks

    for check in run_checks(_resolve_host(host, Path(fleet))):
        mark = "OK  " if check.ok else "FAIL"
        typer.echo(f"[{mark}] {check.name}: {check.detail}")


def _resolve_host(name: str, fleet_path: Path):
    from nira.core.model import load_hosts

    hosts = load_hosts(fleet_path / "inventory")
    for h in hosts:
        if h.name == name:
            return h
    raise typer.BadParameter(f"host {name!r} not in inventory")


def _render_plan(p) -> None:
    for e in p.entries:
        mark = {"ok": "OK", "missing": "INSTALL", "drifted": "DRIFT", "protected": "PROT", "skipped": "SKIP"}[
            str(e.status).split(".")[-1].lower()
        ]
        typer.echo(f"[{mark:7}] {e.bundle}/{e.component} {e.detail}")


if __name__ == "__main__":
    app()
