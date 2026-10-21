"""Regression tests for the seam-audit P0s: protected overwrite, globs, exit codes."""

from __future__ import annotations

from typing import Any

import pytest

from nira.core.model import HostConfig, Op, Plan, PlanEntry, Status
from nira.core.protect import is_protected
from nira.core.results import Result, is_success
from nira.ops import executors


def test_glob_protection_matches_brioso_units() -> None:
    assert is_protected("brioso-api")
    assert is_protected("bifrost") and is_protected("bifrost-daemon")
    assert is_protected("custom-1", ["custom-*"])
    assert not is_protected("git") and not is_protected("/home/x/.config/bifrost/config.json")


def test_planner_marks_glob_protected(monkeypatch: pytest.MonkeyPatch) -> None:
    from nira.core import planner
    from nira.core.bundle import REGISTRY, Bundle, Requirement

    class _Svc(Bundle):
        name = "p0-svc"

        def requirements(self, host: Any) -> Any:
            for n in ("brioso-api", "bifrost", "plain"):
                yield Requirement(n, "service", {"unit_path": f"/etc/systemd/system/{n}.service"})

    monkeypatch.setitem(REGISTRY, "p0-svc", _Svc)
    host = HostConfig(
        "h", "u", "a", "linux", bundles=["p0-svc"], protected_components=["brioso-*"]
    )
    monkeypatch.setattr(planner, "collect_facts", lambda h, reqs=None: {})
    plan = planner.build_plan(host)
    status = {e.component: e.status for e in plan.entries}
    assert status["brioso-api"] == Status.PROTECTED  # host glob
    assert status["bifrost"] == Status.PROTECTED  # built-in bifrost* protection
    assert status["plain"] == Status.MISSING


def test_apply_skips_protected_without_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    from nira.ops.apply import apply_plan

    monkeypatch.setattr(
        "nira.ops.executors.execute", lambda *a, **k: pytest.fail("executor called")
    )
    plan = Plan(host="h", protected_patterns=("brioso-*",))
    plan.add(PlanEntry("b", "brioso-api", Op.INSTALL, Status.MISSING, kind="service"))
    plan.add(PlanEntry("b", "bifrost", Op.INSTALL, Status.MISSING, kind="service"))
    res = apply_plan(plan)
    assert [r["result"] for r in res] == ["ok", "ok"]


def test_result_success_semantics() -> None:
    for v in ("ok", "changed", "unchanged", "skipped", Result.CHANGED):
        assert is_success(v)
    for v in ("failed", "applied", "", "weird"):
        assert not is_success(v)


def _ctx() -> executors.ExecCtx:
    return executors.ExecCtx(os="linux", ssh_user="deb")


def test_unit_put_refuses_empty_content() -> None:
    with pytest.raises(executors.ExecError, match="empty unit"):
        executors._put_unit("svc", "/etc/systemd/system/x.service", {}, _ctx(), lambda p: False)


def test_unit_put_never_clobbers_existing() -> None:
    unit = "/etc/systemd/system/x.service"
    params = {"unit_content": "x"}
    assert executors._put_unit("svc", unit, params, _ctx(), lambda p: True) == []
    adopted = executors._put_unit("svc", unit, {**params, "adopt": True}, _ctx(), lambda p: True)
    assert len(adopted) == 1 and adopted[0].op == "files.put"


def test_apply_exit_zero_on_changed() -> None:
    """Executor's 'changed' must not count as a CLI failure."""
    results = [{"result": "changed"}, {"result": "ok"}]
    assert sum(1 for r in results if not is_success(r["result"])) == 0
