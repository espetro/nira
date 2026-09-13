"""CLI tests via typer.testing.CliRunner. Planner and ops are mocked; no network/ssh."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nira.cli.main import app
from nira.core.model import HostConfig, Op, Plan, PlanEntry, Status

runner = CliRunner()


def make_host(**kw) -> HostConfig:
    d = {
        "name": "h1",
        "ssh_user": "admin",
        "address": "h1.example",
        "os": "macos",
        "bundles": ["base"],
        "protected_components": [],
        "local": False,
    }
    d.update(kw)
    return HostConfig.from_dict(d)


def make_plan(status: Status = Status.OK, protected_status: Status = Status.PROTECTED) -> Plan:
    p = Plan(host="h1")
    p.add(PlanEntry(bundle="base", component="ripgrep", op=Op.INSTALL, status=status))
    p.add(
        PlanEntry(
            bundle="svc",
            component="postgres",
            op=Op.VERIFY,
            status=protected_status,
            detail="protected: report-only",
        )
    )
    return p


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    inv = tmp_path / "inventory"
    inv.mkdir()
    (inv / "hosts.py").write_text(
        "HOSTS = [{'name': 'h1', 'ssh_user': 'admin', 'address': 'h1.example',"
        " 'os': 'macos', 'bundles': ['base'], 'protected_components': ['postgres']}]\n"
    )
    (tmp_path / "manifest.yaml").write_text("bundles: {}\n")
    return tmp_path


@pytest.fixture
def patch_planner():
    plan = make_plan()
    with patch("nira.core.planner.build_plan", return_value=plan) as m:
        yield m


def invoke(args, env=None):
    return runner.invoke(app, args, env=env)


class TestVersion:
    def test_version_flag(self):
        r = invoke(["--version"])
        assert r.exit_code == 0
        assert "nira" in r.output


class TestPlan:
    def test_plan_table(self, fleet, patch_planner):
        r = invoke(["plan", "--host", "h1", "--fleet", str(fleet)])
        assert r.exit_code == 0
        assert "ripgrep" in r.output
        assert "PROT" in r.output

    def test_plan_json(self, fleet, patch_planner):
        r = invoke(["plan", "--host", "h1", "--fleet", str(fleet), "--json"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["host"] == "h1"
        assert len(data["entries"]) == 2

    def test_plan_unknown_host_exit_2(self, fleet, patch_planner):
        r = invoke(["plan", "--host", "ghost", "--fleet", str(fleet)])
        assert r.exit_code == 2

    def test_plan_missing_inventory_exit_2(self, tmp_path, patch_planner):
        r = invoke(["plan", "--host", "h1", "--fleet", str(tmp_path)])
        assert r.exit_code == 2

    def test_plan_fleet_from_env(self, fleet, patch_planner):
        r = invoke(["plan", "--host", "h1"], env={"NIRA_FLEET": str(fleet)})
        assert r.exit_code == 0

    def test_plan_exit_1_when_changes(self, fleet):
        with patch(
            "nira.core.planner.build_plan",
            return_value=make_plan(status=Status.DRIFTED),
        ):
            r = invoke(["plan", "--host", "h1", "--fleet", str(fleet), "--json"])
        assert r.exit_code == 1


def install_fake_ops(module: str, **attrs) -> None:
    """Register a fake nira.ops submodule (the real ones may not exist yet)."""

    mod = types.ModuleType(f"nira.ops.{module}")
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[f"nira.ops.{module}"] = mod
    setattr(sys.modules["nira.ops"], module, mod)


class TestApply:
    def test_apply_dry_run_does_not_import_ops(self, fleet, patch_planner):
        import sys

        saved = sys.modules.pop("nira.ops.apply", None)
        try:
            # make any accidental import explode
            sys.modules["nira.ops.apply"] = None
            r = invoke(["apply", "--host", "h1", "--fleet", str(fleet), "--dry-run"])
            assert r.exit_code == 0
            assert "ripgrep" in r.output
        finally:
            if saved is not None:
                sys.modules["nira.ops.apply"] = saved
            else:
                sys.modules.pop("nira.ops.apply", None)

    def test_apply_success(self, fleet, patch_planner):
        results = [
            {"bundle": "base", "component": "ripgrep", "result": "applied", "detail": ""},
            {"bundle": "svc", "component": "postgres", "result": "skipped", "detail": "protected"},
        ]
        with patch("nira.ops.apply.apply_plan", return_value=results) as m:
            r = invoke(["apply", "--host", "h1", "--fleet", str(fleet), "--json"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["failures"] == 0
        assert len(data["results"]) == 2
        assert m.call_count == 1

    def test_apply_failure_exit_1(self, fleet, patch_planner):
        results = [{"bundle": "base", "component": "x", "result": "failed", "detail": "boom"}]
        with patch("nira.ops.apply.apply_plan", return_value=results):
            r = invoke(["apply", "--host", "h1", "--fleet", str(fleet)])
        assert r.exit_code == 1
        assert "1 failure(s)" in r.output


class TestAssess:
    def test_assess_reports_protected_without_force(self, fleet):
        with patch(
            "nira.core.planner.build_plan",
            return_value=make_plan(status=Status.DRIFTED, protected_status=Status.DRIFTED),
        ):
            r = invoke(["assess", "--host", "h1", "--fleet", str(fleet)])
        assert r.exit_code == 1
        assert "--force" in r.output
        assert "postgres" in r.output

    def test_assess_force_allows_protected(self, fleet):
        with patch(
            "nira.core.planner.build_plan",
            return_value=make_plan(status=Status.DRIFTED, protected_status=Status.DRIFTED),
        ):
            r = invoke(["assess", "--host", "h1", "--fleet", str(fleet), "--force", "--json"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["forced"] is True
        assert len(data["drifted"]) == 2

    def test_assess_no_drift_exit_0(self, fleet, patch_planner):
        r = invoke(["assess", "--host", "h1", "--fleet", str(fleet), "--json"])
        assert r.exit_code == 0
        data = json.loads(r.output)
        assert data["drifted"] == []


class TestDoctor:
    def test_doctor_all_ok(self, fleet):
        @dataclass
        class C:
            name: str
            ok: bool
            detail: str

        install_fake_ops("doctor", run_checks=lambda h: [C("ssh", True, "connected"), C("tailscale", False, "offline")])
        r = invoke(["doctor", "--host", "h1", "--fleet", str(fleet), "--json"])
        assert r.exit_code == 1
        data = json.loads(r.output)
        assert data["failures"] == 1
        assert data["checks"][0]["name"] == "ssh"

    def test_doctor_human_output(self, fleet):
        @dataclass
        class C:
            name: str
            ok: bool
            detail: str

        install_fake_ops("doctor", run_checks=lambda h: [C("ssh", True, "ok")])
        r = invoke(["doctor", "--host", "h1", "--fleet", str(fleet)])
        assert r.exit_code == 0
        assert "OK" in r.output and "ssh" in r.output


class TestInit:
    def test_init_scaffolds(self, tmp_path):
        target = tmp_path / "fleet"
        r = invoke(["init", "--fleet", str(target)])
        assert r.exit_code == 0
        assert (target / "inventory" / "hosts.py").exists()
        assert (target / "group_data").is_dir()
        assert (target / "manifest.yaml").exists()
        assert (target / ".sops.yaml").exists()

    def test_init_refuses_nonempty(self, tmp_path):
        target = tmp_path / "fleet"
        target.mkdir()
        (target / "x.txt").write_text("data")
        r = invoke(["init", "--fleet", str(target)])
        assert r.exit_code == 2

    def test_init_from_env(self, tmp_path):
        target = tmp_path / "fleet"
        r = invoke(["init"], env={"NIRA_FLEET": str(target)})
        assert r.exit_code == 0
