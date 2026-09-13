"""Planner: facts -> diff -> Plan. Gap-filling only; protected components are report-only."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig, Plan, PlanEntry


def collect_facts(host: "HostConfig") -> dict:
    """Collect facts via pyinfra (two-phase). Implemented in nira.ops.facts."""
    from nira.ops.facts import collect  # noqa: PLC0415

    return collect(host)


def build_plan(host: "HostConfig") -> "Plan":
    from nira.core.bundle import REGISTRY
    from nira.core.model import Op, Plan, PlanEntry, Status

    plan = Plan(host=host.name)
    facts = collect_facts(host)
    for bundle_name in host.bundles:
        bundle = REGISTRY.get(bundle_name)
        if bundle is None or not bundle.supports(host):
            plan.add(
                PlanEntry(
                    bundle=bundle_name,
                    component="*",
                    op=Op.SKIP,
                    status=Status.SKIPPED,
                    detail="bundle unavailable for this OS" if bundle else "unknown bundle",
                )
            )
            continue
        b = bundle()
        for entry in b.plan(host, facts):
            if entry.component in host.protected_components:
                entry.status = Status.PROTECTED
                entry.op = Op.VERIFY
                entry.detail = (entry.detail + " ").strip() + "protected: report-only"
            plan.add(entry)
    return plan
