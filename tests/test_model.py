"""Tests for nira.core.model: Plan/PlanEntry/HostConfig/load_manifest/load_hosts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nira.core.model import (
    HostConfig,
    Op,
    Plan,
    PlanEntry,
    Status,
    load_hosts,
    load_manifest,
)


def make_entry(status: Status = Status.OK, component: str = "c1") -> PlanEntry:
    return PlanEntry(bundle="b", component=component, op=Op.INSTALL, status=status)


class TestPlanEntry:
    def test_to_dict_roundtrip_fields(self):
        e = PlanEntry(bundle="base", component="ripgrep", op=Op.INSTALL, status=Status.MISSING, detail="absent")
        d = e.to_dict()
        assert d == {
            "bundle": "base",
            "component": "ripgrep",
            "op": "install",
            "status": "missing",
            "detail": "absent",
        }

    def test_str_enum_values(self):
        assert str(Op.CONFIG) == "config"
        assert str(Status.DRIFTED) == "drifted"


class TestPlan:
    def test_changes_counts_missing_and_drifted_only(self):
        p = Plan(host="h")
        p.add(make_entry(Status.OK))
        p.add(make_entry(Status.MISSING))
        p.add(make_entry(Status.DRIFTED, "c3"))
        p.add(make_entry(Status.PROTECTED, "c4"))
        p.add(make_entry(Status.SKIPPED, "c5"))
        assert [e.component for e in p.changes] == ["c1", "c3"]

    def test_changes_strict(self):
        p = Plan(host="h")
        p.add(make_entry(Status.MISSING, "a"))
        p.add(make_entry(Status.DRIFTED, "b"))
        p.add(make_entry(Status.OK, "c"))
        assert len(p.changes) == 2

    def test_to_json_is_valid_json(self):
        p = Plan(host="h")
        p.add(make_entry(Status.OK))
        data = json.loads(p.to_json())
        assert data["host"] == "h"
        assert data["changes"] == 0
        assert len(data["entries"]) == 1


class TestHostConfig:
    def test_from_dict_minimal(self):
        h = HostConfig.from_dict(
            {"name": "h1", "ssh_user": "u", "address": "h1.example", "os": "linux"}
        )
        assert h.bundles == []
        assert h.protected_components == []
        assert h.local is False

    def test_from_dict_full(self):
        h = HostConfig.from_dict(
            {
                "name": "kino",
                "ssh_user": "admin",
                "address": "kino.ts.net",
                "os": "macos",
                "bundles": ["base"],
                "protected_components": ["postgres"],
                "local": True,
            }
        )
        assert h.local is True
        assert h.protected_components == ["postgres"]


class TestLoaders:
    def test_load_manifest(self, tmp_path: Path):
        f = tmp_path / "manifest.yaml"
        f.write_text("bundles:\n  base:\n    packages: [ripgrep]\n")
        m = load_manifest(f)
        assert m["bundles"]["base"]["packages"] == ["ripgrep"]

    def test_load_manifest_missing_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_manifest(tmp_path / "nope.yaml")

    def test_load_hosts_parses_inventory_py(self, tmp_path: Path):
        inv = tmp_path / "inventory"
        inv.mkdir()
        (inv / "hosts.py").write_text(
            "HOSTS = [{'name': 'a', 'ssh_user': 'u', 'address': 'a', 'os': 'macos'}]\n"
        )
        hosts = load_hosts(inv)
        assert len(hosts) == 1
        assert hosts[0].name == "a"

    def test_load_hosts_sorted_and_merged(self, tmp_path: Path):
        inv = tmp_path / "inventory"
        inv.mkdir()
        (inv / "zeta.py").write_text(
            "HOSTS = [{'name': 'z', 'ssh_user': 'u', 'address': 'z', 'os': 'linux'}]\n"
        )
        (inv / "alpha.py").write_text(
            "HOSTS = [{'name': 'a', 'ssh_user': 'u', 'address': 'a', 'os': 'linux'}]\n"
        )
        assert [h.name for h in load_hosts(inv)] == ["a", "z"]

    def test_load_hosts_ignores_non_py(self, tmp_path: Path):
        inv = tmp_path / "inventory"
        inv.mkdir()
        (inv / "notes.txt").write_text("HOSTS = ['garbage']")
        assert load_hosts(inv) == []
