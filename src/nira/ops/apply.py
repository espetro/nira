"""Apply a plan: execute only missing/drifted entries via pyinfra operations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nira.core.model import Plan

from nira.core.protect import is_protected
from nira.core.results import Result


def _result(entry: Any, result: Result, detail: str = "") -> dict[str, Any]:
    return {
        "bundle": entry.bundle,
        "component": entry.component,
        "result": str(result),
        "detail": detail,
    }


def apply_plan(plan: Plan, dry_run: bool = False, verify: bool = True) -> list[dict[str, Any]]:
    """Apply only entries with status missing/drifted. Returns per-entry results."""
    results: list[dict[str, Any]] = []
    changed: list[Any] = []

    for entry in plan.entries:
        if entry.status not in ("missing", "drifted"):
            continue  # gap-fill semantics: ok/skipped/protected produce no ops
        if is_protected(entry.component, plan_protected(plan)):
            results.append(_result(entry, Result.OK, "protected: report-only, skipped"))
            continue
        if dry_run:
            results.append(_result(entry, Result.OK, "dry-run: would apply"))
            continue
        changed.append(entry)

    if dry_run:
        return results

    if changed:
        _apply_via_pyinfra(_resolve_host(plan), changed, results, verify=verify)
    return results


def _resolve_host(plan: Plan) -> Any:
    """Resolve the plan's host to (pyinfra host name, host data) with fleet context."""
    from nira.ops.inventory import host_inventory

    if not isinstance(plan.host, str):
        return host_inventory(plan.host)
    # plan.host holds the host name; look up the full HostConfig via the fleet
    # path carried on the plan when built by the CLI.
    fleet = getattr(plan, "fleet_path", None)
    if fleet:
        from nira.core.model import load_hosts

        for h in load_hosts(Path(fleet) / "inventory"):
            if h.name == plan.host:
                target, data = host_inventory(h)
                return target, _with_fleet_context(data, h, Path(fleet))
    # no fleet context: pass the raw name through (tests/advanced callers
    # monkeypatch host_inventory anyway)
    return host_inventory(plan.host)


def _with_fleet_context(data: dict[str, Any], host: Any, fleet: Path) -> dict[str, Any]:
    """Add fleet path and template variables to the pyinfra host data."""
    variables = {"SSH_USER": host.ssh_user, "HOST": host.name}
    manifest = fleet / "manifest.yaml"
    if manifest.is_file():
        from nira.core.model import load_manifest

        domain = ((load_manifest(manifest) or {}).get("fleet") or {}).get("tailscale_domain")
        if domain:
            variables["TAILNET_DOMAIN"] = domain
    return {**data, "fleet_path": str(fleet), "template_vars": variables}


def _apply_via_pyinfra(
    host: Any, entries: list[Any], results: list[dict], verify: bool = True
) -> None:
    """Execute operations through pyinfra, one fresh state per entry."""
    from nira.ops.executors import execute

    execute(host, entries, results, verify=verify)


def plan_protected(plan: Plan) -> tuple[str, ...]:
    """Host-supplied protected patterns carried on the plan (empty if unknown)."""
    return tuple(getattr(plan, "protected_patterns", ()) or ())
