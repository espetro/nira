"""Apply a plan: execute only missing/drifted entries via pyinfra operations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nira.core.model import Plan

# Components that must never be mutated, only verified.
PROTECTED_PATTERNS = ("web-services", "brioso-", "xvfb-orca", "orca.service", "orcad")


def is_protected(name: str) -> bool:
    return any(p in name for p in PROTECTED_PATTERNS)


def _result(entry: Any, result: str, detail: str = "") -> dict[str, Any]:
    return {
        "bundle": entry.bundle,
        "component": entry.component,
        "result": result,
        "detail": detail,
    }


def apply_plan(plan: Plan, dry_run: bool = False) -> list[dict[str, Any]]:
    """Apply only entries with status missing/drifted. Returns per-entry results."""
    results: list[dict[str, Any]] = []
    changed: list[Any] = []

    for entry in plan.entries:
        if entry.status not in ("missing", "drifted"):
            continue  # gap-fill semantics: ok/skipped/protected produce no ops
        if is_protected(entry.component):
            results.append(_result(entry, "ok", "protected: report-only, skipped"))
            continue
        if dry_run:
            results.append(_result(entry, "ok", "dry-run: would apply"))
            continue
        changed.append(entry)

    if dry_run:
        return results

    host = _resolve_host(plan)
    if changed:
        _apply_via_pyinfra(host, changed, results)
    return results


def _resolve_host(plan: Plan) -> Any:
    """Resolve the plan's host to a pyinfra host data dict / inventory tuple."""
    from nira.ops.inventory import host_inventory

    if not isinstance(plan.host, str):
        return host_inventory(plan.host)
    # plan.host holds the host name; look up the full HostConfig via env-provided
    # fleet path carried on the plan when built by the CLI.
    fleet = getattr(plan, "fleet_path", None)
    if fleet:
        from nira.core.model import load_hosts

        for h in load_hosts(Path(fleet) / "inventory"):
            if h.name == plan.host:
                return host_inventory(h)
    # no fleet context: pass the raw name through (tests/advanced callers
    # monkeypatch host_inventory anyway)
    return host_inventory(plan.host)


def _apply_via_pyinfra(host: Any, entries: list[Any], results: list[dict]) -> None:
    """Execute operations through pyinfra, grouped by kind."""
    from nira.ops.executors import execute

    execute(host, entries, results)
