"""Tests for nira.ops: facts, apply, doctor. No ssh/network/sudo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import nira.ops.facts as facts_mod
from nira.core.model import Op, Plan, PlanEntry, Status
from nira.ops.apply import apply_plan, is_protected
from nira.ops.doctor import CheckResult, run_checks
from nira.ops.facts import KINDS, collect


def make_host(**kw: Any) -> Any:
    from nira.core.model import HostConfig

    defaults = dict(
        name="test", ssh_user="u", address="1.2.3.4", os="linux", local=False
    )
    defaults.update(kw)
    return HostConfig(**defaults)


# ---------------- facts.collect ----------------


def test_collect_dry_all_kinds_missing() -> None:
    facts = collect(make_host(), dry=True)
    assert set(facts) == set(KINDS)
    assert all(v == {} for v in facts.values())


def test_collect_dry_never_imports_pyinfra(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "nira.ops.pyinfra_facts", None)
    facts = collect(make_host(), dry=True)
    assert facts["package"] == {}


def test_collect_delegates_to_pyinfra_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(host: Any) -> dict:
        return {"package": {"git": True}}

    monkeypatch.setattr(
        "nira.ops.pyinfra_facts.collect_via_pyinfra", fake
    )
    facts = collect(make_host())
    assert facts["package"] == {"git": True}
    assert "file" in facts  # kinds guaranteed present


def test_local_host_facts_report_nono(monkeypatch: pytest.MonkeyPatch) -> None:
    # smoke: the local collector function itself, not touching real PATH assumptions
    from nira.ops.pyinfra_facts import KINDS as PK, _collect_local

    assert set(_collect_local()) <= set(PK) | set(KINDS)


# ---------------- apply_plan: gap-fill semantics ----------------


@dataclass
class FakeEntry:
    bundle: str
    component: str
    op: Op
    status: Status
    detail: str = ""
    kind: str = "package"
    params: dict = field(default_factory=dict)


def _plan(*entries: PlanEntry | FakeEntry) -> Plan:
    p = Plan(host="test")
    for e in entries:
        p.add(e)  # type: ignore[arg-type]
    return p


def test_ok_entries_never_produce_ops() -> None:
    plan = _plan(FakeEntry("core", "git", Op.INSTALL, Status.OK))
    results = apply_plan(plan, dry_run=True)
    assert results == []


def test_skipped_and_protected_produce_no_ops() -> None:
    plan = _plan(
        FakeEntry("core", "x", Op.SKIP, Status.SKIPPED),
        FakeEntry("core", "brioso-web", Op.INSTALL, Status.MISSING, kind="service"),
    )
    results = apply_plan(plan, dry_run=True)
    # protected component is intercepted before dry-run would apply it
    assert results == [
        {
            "bundle": "core",
            "component": "brioso-web",
            "result": "ok",
            "detail": "protected: report-only, skipped",
        }
    ]


def test_dry_run_reports_would_apply() -> None:
    plan = _plan(
        FakeEntry("core", "git", Op.INSTALL, Status.MISSING),
        FakeEntry("core", "cfg", Op.CONFIG, Status.DRIFTED, kind="file"),
    )
    results = apply_plan(plan, dry_run=True)
    assert len(results) == 2
    assert all(r["result"] == "ok" and "dry-run" in r["detail"] for r in results)
    assert [r["component"] for r in results] == ["git", "cfg"]


def test_protected_components_report_only() -> None:
    for name in ("web-services", "brioso-api", "xvfb-orca", "orca.service", "orcad"):
        assert is_protected(name)
    assert not is_protected("git")


def test_apply_runs_executor_for_changed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def fake_execute(host: Any, entries: list, results: list) -> None:
        called.extend(e.component for e in entries)
        results.extend(
            {"bundle": e.bundle, "component": e.component, "result": "changed", "detail": ""}
            for e in entries
        )

    monkeypatch.setattr("nira.ops.executors.execute", fake_execute)
    monkeypatch.setattr(
        "nira.ops.inventory.host_inventory", lambda name: ("@local", {})
    )
    plan = _plan(
        FakeEntry("core", "ok-pkg", Op.INSTALL, Status.OK),
        FakeEntry("core", "missing-pkg", Op.INSTALL, Status.MISSING),
    )
    results = apply_plan(plan)
    assert called == ["missing-pkg"]
    assert results[0]["result"] == "changed"


# ---------------- doctor ----------------


def test_check_result_shape() -> None:
    cr = CheckResult("ssh", True, "fine")
    assert cr.name == "ssh" and cr.ok and cr.detail == "fine"


def test_run_checks_local_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    import nira.ops.doctor as doc

    monkeypatch.setattr(doc.shutil, "which", lambda b: None)
    host = make_host(os="linux", local=True)
    checks = run_checks(host)
    names = [c.name for c in checks]
    assert {"ssh", "tailscale", "systemd", "orca-pairing", "sops"} <= set(names)
    assert all(isinstance(c, CheckResult) for c in checks)
    ssh = next(c for c in checks if c.name == "ssh")
    assert ssh.ok  # local host: no ssh needed
