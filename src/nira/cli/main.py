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
from nira.core.protect import is_protected
from nira.core.results import is_success

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
    from nira.core.bundle import RequirementError
    from nira.core.planner import build_plan
    from nira.ops.pyinfra_facts import HostUnreachable, ProbeError

    try:
        return build_plan(host)
    except HostUnreachable as exc:
        typer.secho(f"host unreachable: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc
    except (ProbeError, RequirementError) as exc:
        typer.secho(f"cannot plan {host.name}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


def _ok(failures: int) -> None:
    raise typer.Exit(code=0 if failures == 0 else 1)


@app.command()
def plan(
    host: str | None = typer.Option(None, "--host", help="Host name from inventory"),
    fleet: str = FleetOpt,
    json_output: bool = typer.Option(False, "--json"),
    all_hosts: bool = typer.Option(False, "--all-hosts", help="Every host in the inventory"),
    check: bool = typer.Option(
        False, "--check", help="With --all-hosts: offline render/validate, no ssh"
    ),
    probe: bool = typer.Option(False, "--probe", help="With --check: also probe hosts over ssh"),
) -> None:
    """Show the gap between manifest and host state. Installs nothing."""
    if all_hosts or check or probe:
        if not (all_hosts and check) or host:
            typer.secho("use: plan --all-hosts --check [--probe] (without --host)", fg="red", err=True)
            raise typer.Exit(code=2)
        _plan_check(Path(fleet), json_output, probe)
        return
    if not host:
        typer.secho("missing option '--host' (or use --all-hosts --check)", fg="red", err=True)
        raise typer.Exit(code=2)
    p = _load_plan(_resolve_host(host, Path(fleet)))
    if json_output:
        typer.echo(p.to_json())
    else:
        render_plan(p)
    _ok(len(p.changes))


def _plan_check(fleet: Path, json_output: bool, probe: bool) -> None:
    from nira.cli.check import check_fleet
    from nira.cli.render import render_check

    report = check_fleet(fleet, probe=probe)
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "hosts": report.hosts,
                    "errors": len(report.errors),
                    "issues": [i.to_dict() for i in report.issues],
                },
                indent=2,
            )
        )
    else:
        render_check(report)
    _ok(len(report.errors))


@app.command()
def apply(
    host: str = typer.Option(..., "--host"),
    fleet: str = FleetOpt,
    json_output: bool = typer.Option(False, "--json"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; apply nothing"),
    verify: bool = typer.Option(
        True, "--verify/--no-verify", help="Verify every entry after applying (default on)"
    ),
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
    p.fleet_path = fleet
    p.protected_patterns = tuple(hc.protected_components)
    results = apply_plan(p, verify=verify)
    failures = sum(1 for r in results if not is_success(r.get("result", "")))
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
    hc = _resolve_host(host, Path(fleet))
    protected = [e for e in drifted if is_protected(e.component, hc.protected_components)]
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
