"""nira CLI: plan / apply / assess / doctor / init.

Exit codes: 0 ok, 1 failures, 2 usage (typer handles usage).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from nira.cli.render import render_plan, render_results
from nira.core.model import HostConfig, Plan, Status

app = typer.Typer(help="nira: manifest-first, idempotent fleet replicator", no_args_is_help=True)

FleetOpt = typer.Option(..., "--fleet", envvar="NIRA_FLEET", help="Path to fleet repo")


def _version(value: bool) -> None:
    if value:
        from nira import __version__

        typer.echo(f"nira {__version__}")
        raise typer.Exit


@app.callback()
def _main(
    version: bool = typer.Option(False, "--version", callback=_version, is_eager=True),
) -> None:
    """nira: manifest-first, idempotent fleet replicator."""


def _resolve_host(name: str, fleet_path: Path) -> HostConfig:
    from nira.core.model import load_hosts

    inv = fleet_path / "inventory"
    if not inv.is_dir():
        typer.secho(f"inventory dir not found: {inv}", fg="red", err=True)
        raise typer.Exit(code=2)
    for h in load_hosts(inv):
        if h.name == name:
            return h
    typer.secho(f"host {name!r} not in inventory", fg="red", err=True)
    raise typer.Exit(code=2)


def _load_plan(host: HostConfig) -> Plan:
    from nira.core.planner import build_plan

    return build_plan(host)


def _ok(failures: int) -> None:
    raise typer.Exit(code=0 if failures == 0 else 1)


@app.command()
def plan(
    host: str = typer.Option(..., "--host", help="Host name from inventory"),
    fleet: str = FleetOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Show the gap between manifest and host state. Installs nothing."""
    p = _load_plan(_resolve_host(host, Path(fleet)))
    if json_output:
        typer.echo(p.to_json())
    else:
        render_plan(p)
    _ok(len(p.changes))


@app.command()
def apply(
    host: str = typer.Option(..., "--host"),
    fleet: str = FleetOpt,
    json_output: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; apply nothing"),
) -> None:
    """Apply only the gap from plan. Re-run must produce zero changes."""
    hc = _resolve_host(host, Path(fleet))
    if dry_run:
        p = _load_plan(hc)
        if json_output:
            typer.echo(p.to_json())
        else:
            render_plan(p)
        _ok(len(p.changes))
        return
    from nira.ops.apply import apply_plan

    p = _load_plan(hc)
    results = apply_plan(p)
    failures = sum(1 for r in results if str(r.get("result", "")).lower() not in ("ok", "success", "applied", "skipped"))
    if json_output:
        typer.echo(json.dumps({"host": p.host, "failures": failures, "results": results}, indent=2))
    else:
        render_results(results)
        typer.echo(f"{failures} failure(s)")
    _ok(failures)


@app.command()
def assess(
    host: str = typer.Option(..., "--host"),
    fleet: str = FleetOpt,
    force: bool = typer.Option(False, "--force", help="Allow adopting protected components too"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Assess drifted pre-existing services; report adoption actions (nothing applied)."""
    p = _load_plan(_resolve_host(host, Path(fleet)))
    drifted = [e for e in p.entries if e.status == Status.DRIFTED]
    protected = [e for e in drifted if e.component in (_resolve_host(host, Path(fleet)).protected_components)]
    adoptable = [e for e in drifted if e not in protected]

    if protected and not force:
        typer.secho(
            "refusing to adopt protected components: "
            + ", ".join(f"{e.bundle}/{e.component}" for e in protected)
            + " (use --force)",
            fg="red",
            err=True,
        )
        _ok(1)
        return

    report: dict[str, Any] = {
        "host": p.host,
        "drifted": [e.to_dict() for e in drifted],
        "adoptable": [e.to_dict() for e in adoptable],
        "protected": [e.to_dict() for e in protected],
        "forced": force,
    }
    if json_output:
        typer.echo(json.dumps(report, indent=2))
    else:
        for e in adoptable:
            typer.echo(f"ADOPT   {e.bundle}/{e.component} {e.detail}")
        for e in protected:
            typer.echo(f"PROTECT {e.bundle}/{e.component} {e.detail} (report-only)")
        typer.echo(f"{len(adoptable)} adoptable, {len(protected)} protected")
    _ok(len(protected) if protected and not force else 0)


@app.command()
def doctor(
    host: str = typer.Option(..., "--host"),
    fleet: str = FleetOpt,
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Health checks: connectivity, tailscale, pairing state, service status."""
    from nira.ops.doctor import run_checks

    checks = run_checks(_resolve_host(host, Path(fleet)))
    failures = sum(1 for c in checks if not c.ok)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "host": host,
                    "failures": failures,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks
                    ],
                },
                indent=2,
            )
        )
    else:
        for c in checks:
            mark = typer.style("OK  ", fg="green") if c.ok else typer.style("FAIL", fg="red")
            typer.echo(f"[{mark}] {c.name}: {c.detail}")
    _ok(failures)


@app.command()
def init(fleet: str = FleetOpt) -> None:
    """Scaffold a fleet repo skeleton."""
    root = Path(fleet)
    if root.exists() and any(root.iterdir()):
        typer.secho(f"fleet dir not empty: {root}", fg="red", err=True)
        raise typer.Exit(code=2)
    (root / "inventory").mkdir(parents=True, exist_ok=True)
    (root / "group_data").mkdir(parents=True, exist_ok=True)
    (root / "inventory" / "hosts.py").write_text(
        "# HOSTS = [\n"
        "#     {\n"
        '#         "name": "myhost",\n'
        '#         "ssh_user": "admin",\n'
        '#         "address": "myhost.tailnet.ts.net",\n'
        '#         "os": "macos",  # or "linux"\n'
        '#         "bundles": ["base"],\n'
        '#         "protected_components": [],\n'
        '#         "local": False,\n'
        "#     },\n"
        "# ]\n"
    )
    (root / "manifest.yaml").write_text(
        "# Fleet manifest: requirements per bundle.\n"
        "bundles:\n"
        "  base:\n"
        "    packages: []\n"
    )
    (root / ".sops.yaml").write_text(
        "# SOPS config placeholder: point at your age/KMS key.\n"
        "creation_rules: []\n"
    )
    typer.echo(f"scaffolded fleet repo at {root}")


if __name__ == "__main__":
    app()
