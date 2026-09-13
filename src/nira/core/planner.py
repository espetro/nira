"""Planner: facts -> diff -> Plan. Gap-filling only; protected components are report-only."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig, Plan


def collect_facts(host: HostConfig) -> dict:
    """Collect facts via pyinfra (two-phase). Implemented in nira.ops.facts."""
    from nira.ops.facts import collect

    return collect(host)


def build_plan(host: HostConfig) -> Plan:
    import nira.bundles as _bundles  # noqa: F401,PLC0415 - side effect: populate registry
    from nira.core.bundle import REGISTRY
    from nira.core.model import Op, Plan, PlanEntry, Status

    plan = Plan(host=host.name)
    facts = collect_facts(host)
    for bundle_name in host.bundles:
        bundle_cls = REGISTRY.get(bundle_name)
        if bundle_cls is None:
            plan.add(
                PlanEntry(
                    bundle=bundle_name,
                    component="*",
                    op=Op.SKIP,
                    status=Status.SKIPPED,
                    detail="unknown bundle",
                )
            )
            continue
        bundle = bundle_cls()
        if not bundle.supports(host):
            plan.add(
                PlanEntry(
                    bundle=bundle_name,
                    component="*",
                    op=Op.SKIP,
                    status=Status.SKIPPED,
                    detail="bundle unavailable for this OS",
                )
            )
            continue
        for entry in bundle.plan(host, facts):
            if entry.component in host.protected_components:
                entry.status = Status.PROTECTED
                entry.op = Op.VERIFY
                entry.detail = (entry.detail + " ").strip() + "protected: report-only"
            plan.add(entry)
    return plan
