"""Planner: facts -> diff -> Plan. Gap-filling only; protected components are report-only."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nira.core.model import HostConfig, Plan


def collect_facts(host: HostConfig, requirements: Iterable[Any] | None = None) -> dict:
    """Collect facts by probing each requirement. Implemented in nira.ops.facts."""
    from nira.ops.facts import collect

    return collect(host, requirements=requirements)


def _ordered_bundles(host: HostConfig, registry: dict) -> list[str]:
    """host.bundles ordered by bundle priority (lower first); stable for ties."""
    return sorted(
        host.bundles,
        key=lambda n: getattr(registry.get(n), "priority", 50),
    )


def build_plan(host: HostConfig) -> Plan:
    import nira.bundles as _bundles  # noqa: F401 - side effect: populate registry
    from nira.core.bundle import REGISTRY, validate_requirement
    from nira.core.model import Op, Plan, PlanEntry, Status
    from nira.core.protect import is_protected

    plan = Plan(host=host.name)
    ordered = _ordered_bundles(host, REGISTRY)

    # Validate and gather requirements first: malformed requirements fail before any probe.
    requirements: list[Any] = []
    usable: dict[str, Any] = {}
    for bundle_name in ordered:
        bundle_cls = REGISTRY.get(bundle_name)
        if bundle_cls is None:
            continue
        if not getattr(bundle_cls, "enabled", True):
            continue
        bundle = bundle_cls()
        if not bundle.supports(host):
            continue
        usable[bundle_name] = bundle
        for req in bundle.resolved(host):
            validate_requirement(req, host.os)
            requirements.append(req)

    facts = collect_facts(host, requirements)
    for bundle_name in ordered:
        bundle = usable.get(bundle_name)
        if bundle is None:
            cls = REGISTRY.get(bundle_name)
            unknown = cls is None
            disabled = cls is not None and not getattr(cls, "enabled", True)
            plan.add(
                PlanEntry(
                    bundle=bundle_name,
                    component="*",
                    op=Op.SKIP,
                    status=Status.SKIPPED,
                    detail="unknown bundle"
                    if unknown
                    else "disabled"
                    if disabled
                    else "bundle unavailable for this OS",
                )
            )
            continue
        for entry in bundle.plan(host, facts):
            if is_protected(entry.component, host.protected_components):
                entry.status = Status.PROTECTED
                entry.op = Op.VERIFY
                entry.detail = (entry.detail + " ").strip() + "protected: report-only"
            plan.add(entry)
    return plan
