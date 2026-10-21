"""Tests for nira.ops: facts, apply, doctor. No ssh/network/sudo."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nira.core.model import Op, Plan, PlanEntry, Status
from nira.ops.apply import apply_plan, is_protected
from nira.ops.doctor import CheckResult, run_checks
from nira.ops.facts import KINDS, collect


def make_host(**kw: Any) -> Any:
    from nira.core.model import HostConfig

    defaults = {
        "name": "test",
        "ssh_user": "u",
        "address": "1.2.3.4",
        "os": "linux",
        "local": False,
    }
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
    def fake(host: Any, requirements: Any = None) -> dict:
        return {"package": {"git": True}}

    monkeypatch.setattr(
        "nira.ops.pyinfra_facts.collect_via_pyinfra", fake
    )
    facts = collect(make_host())
    assert facts["package"] == {"git": True}
    assert "file" in facts  # kinds guaranteed present


def test_collector_probes_every_kind_and_distinguishes_ssh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from nira.core.bundle import Requirement
    from nira.ops import pyinfra_facts as pf

    reqs = [
        Requirement("git", "package"),
        Requirement("/etc/x", "file"),
        Requirement("/opt/d", "directory"),
        Requirement("svc", "service"),
        Requirement("S", "secret", {"sops_file": "secrets/a.yaml"}),
        Requirement("mise:uv", "custom", {"mise": True, "tool": "uv"}),
    ]
    rcs = [0, 1, 2, 0, 1, 0]
    out = "".join(f"__NIRA__ {i} {rc}\n" for i, rc in enumerate(rcs))
    seen: dict[str, Any] = {}

    def ok_run(argv: list[str], **kw: Any) -> Any:
        seen["script"] = kw["input"]
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", ok_run)
    facts = pf.collect_via_pyinfra(make_host(), reqs)
    assert facts["package"]["git"] is True
    assert facts["file"]["/etc/x"] is False
    assert isinstance(facts["directory"]["/opt/d"], dict)  # present but drifted
    assert facts["service"]["svc"] is True  # active unit reports OK
    assert facts["custom"]["mise:uv"] is True
    assert seen["script"].count("__NIRA__") == len(reqs)
    assert "mise where uv" in seen["script"]  # mise tools probed via mise, not dpkg

    def ssh_dead(argv: list[str], **_kw: Any) -> Any:
        return subprocess.CompletedProcess(argv, 255, stdout="", stderr="no route")

    monkeypatch.setattr(subprocess, "run", ssh_dead)
    with pytest.raises(pf.HostUnreachable):
        pf.collect_via_pyinfra(make_host(), reqs)

    def broken_probe(argv: list[str], **_kw: Any) -> Any:
        return subprocess.CompletedProcess(argv, 0, stdout="__NIRA__ 0 127\n", stderr="")

    monkeypatch.setattr(subprocess, "run", broken_probe)
    with pytest.raises(pf.ProbeError):
        pf.collect_via_pyinfra(make_host(), reqs[:1])


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

    def fake_execute(host: Any, entries: list, results: list, verify: bool = True) -> None:
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


def test_run_checks_runs_against_target_not_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="running", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    host = make_host(os="linux", local=False, address="box.ts.net", ssh_user="deb")
    checks = run_checks(host)
    names = [c.name for c in checks]
    assert {"ssh", "tailscale", "systemd", "orca-pairing", "sops"} <= set(names)
    assert all(isinstance(c, CheckResult) for c in checks)
    assert calls and all(c[0] == "ssh" and "deb@box.ts.net" in c for c in calls)
    assert not any("/Users/" in " ".join(c) for c in calls)  # no hardcoded home


def test_run_checks_local_host_uses_local_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kw: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="running", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    checks = run_checks(make_host(os="linux", local=True))
    assert next(c for c in checks if c.name == "ssh").ok
    assert all(c[0] == "bash" for c in calls)
