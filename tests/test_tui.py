"""Tests for the nira TUI: grid rendering, apply flow, doctor view.

Uses textual pilot with mocked planner/build_plan and synthetic HostConfig /
Plan / PlanEntry objects. nira.ops is absent in this repo, so lazy imports
must degrade gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest

from nira.tui.app import STATUS_COLORS, NiraTui, worst_status


class Status(StrEnum):
    OK = "ok"
    MISSING = "missing"
    DRIFTED = "drifted"
    PROTECTED = "protected"
    SKIPPED = "skipped"


@dataclass
class Entry:
    bundle: str
    component: str
    op: str
    status: Status
    detail: str = ""


@dataclass
class Plan:
    host: str
    entries: list[Entry] = field(default_factory=list)

    @property
    def changes(self) -> list[Entry]:
        return [e for e in self.entries if e.status in (Status.MISSING, Status.DRIFTED)]


@dataclass
class Host:
    name: str
    ssh_user: str = "root"
    address: str = "127.0.0.1"
    os: str = "linux"
    bundles: list[str] = field(default_factory=list)
    local: bool = False


BUNDLES = ["base", "dev", "shell"]


def synthetic_plan(host: str) -> Plan:
    p = Plan(host=host)
    p.entries = [
        Entry("base", "git", "install", Status.OK, "present"),
        Entry("dev", "neovim", "config", Status.DRIFTED, "config drifted"),
        Entry("shell", "zsh", "install", Status.MISSING, "not installed"),
    ]
    return p


def make_hosts() -> list[Host]:
    hosts = []
    for name in ("alpha", "beta"):
        h = Host(name=name, bundles=list(BUNDLES))
        hosts.append(h)
    return hosts


@pytest.fixture
def mock_core(monkeypatch):
    """Patch load_hosts and build_plan with synthetic data."""
    hosts = make_hosts()
    import nira.tui.app as app_mod

    monkeypatch.setattr(app_mod, "load_hosts", lambda *_: hosts)
    monkeypatch.setattr(app_mod, "build_plan", lambda host: synthetic_plan(host.name))
    return hosts


@pytest.fixture
def app(mock_core):
    return NiraTui(fleet_dir=Path("/tmp/fake-fleet"))


async def _pilot(app):
    async with app.run_test() as pilot:
        yield pilot


# ---------------------------------------------------------------- grid


async def test_grid_renders_all_hosts_and_bundles(app) -> None:
    async with app.run_test():
        grid = app.query_one("#grid")
        assert grid.row_count == 2
        assert len(grid.columns) == len(BUNDLES)


async def test_grid_cell_colors_match_status(app) -> None:
    async with app.run_test():
        grid = app.query_one("#grid")
        # row keys are host names
        assert set(grid.rows) == {"alpha", "beta"}
        # worst of ok/drifted/missing -> missing (red)
        cell = grid.get_cell_at(grid.get_cell_coordinate("beta", "shell"))
        assert STATUS_COLORS["missing"] in str(cell)
        # base bundle -> ok (green)
        assert STATUS_COLORS["ok"] in str(grid.get_cell_at(grid.get_cell_coordinate("alpha", "base")))
        # dev bundle -> drifted (yellow)
        assert STATUS_COLORS["drifted"] in str(grid.get_cell_at(grid.get_cell_coordinate("alpha", "dev")))


async def test_worst_status_ordering() -> None:
    assert worst_status([Entry("b", "c", "i", Status.OK)]) == "ok"
    assert worst_status([Entry("b", "c", "i", Status.OK), Entry("b", "d", "i", Status.DRIFTED)]) == "drifted"
    assert (
        worst_status(
            [Entry("b", "c", "i", Status.DRIFTED), Entry("b", "d", "i", Status.MISSING)]
        )
        == "missing"
    )


# ---------------------------------------------------------------- plan preview


async def test_plan_preview_shows_entries(app) -> None:
    async with app.run_test():
        table = app.query_one("#entries")
        assert table.row_count == 3
        line = app.query_one("#statusline")
        text = line.content
        assert "alpha" in text
        assert "2 changes" in text


async def test_row_highlight_switches_host(app) -> None:
    async with app.run_test() as pilot:
        grid = app.query_one("#grid")
        grid.focus()
        await pilot.press("down")
        await pilot.pause()
        line = app.query_one("#statusline").content
        assert "beta" in line
        table = app.query_one("#entries")
        assert table.row_count == 3


# ---------------------------------------------------------------- refresh


async def test_refresh_rebuilds_plans(app, mock_core, monkeypatch) -> None:
    calls = {"n": 0}

    import nira.tui.app as app_mod

    def counting_plan(host):
        calls["n"] += 1
        return synthetic_plan(host.name)

    monkeypatch.setattr(app_mod, "build_plan", counting_plan)
    async with app.run_test() as pilot:
        assert calls["n"] == 2
        app.action_refresh()
        await pilot.pause()
        assert calls["n"] == 4


async def test_refresh_survives_planner_failure(app, mock_core, monkeypatch) -> None:
    import nira.tui.app as app_mod

    def boom(host):
        raise RuntimeError("planner down")

    monkeypatch.setattr(app_mod, "build_plan", boom)
    a = NiraTui(fleet_dir=Path("/tmp/fake-fleet"))
    async with a.run_test():
        assert a.state.plans["alpha"] is None
        grid = a.query_one("#grid")
        # no crash; cells fall back to skipped styling
        assert grid.row_count == 2


# ---------------------------------------------------------------- apply


async def test_apply_requires_confirmation(app, monkeypatch) -> None:
    applied = []
    import nira.tui.app as app_mod

    def fake_apply(plan):
        applied.append(plan.host)
        return iter(["installed git", "fixed neovim"])

    import sys
    import types

    mod = types.ModuleType("nira.ops.apply")
    mod.apply_plan = fake_apply
    monkeypatch.setitem(sys.modules, "nira.ops.apply", mod)

    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        # modal is on top, nothing applied yet
        assert applied == []
        assert isinstance(app.screen, app_mod.ConfirmScreen)
        # confirm with 'y'
        await pilot.press("y")
        await pilot.pause()
        assert applied == ["alpha"]
        assert app.apply_results == ["installed git", "fixed neovim"]
        assert app.applied_host == "alpha"


async def test_apply_cancel_does_nothing(app, monkeypatch) -> None:
    import sys
    import types

    applied = []

    def fake_apply(plan):
        applied.append(plan.host)
        return iter([])

    mod = types.ModuleType("nira.ops.apply")
    mod.apply_plan = fake_apply
    monkeypatch.setitem(sys.modules, "nira.ops.apply", mod)

    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        assert applied == []
        assert app.apply_results is None


async def test_apply_stream_renders_results(app, monkeypatch) -> None:
    import sys
    import types

    mod = types.ModuleType("nira.ops.apply")
    mod.apply_plan = lambda plan: iter(["ok: git", "ok: neovim", "fail: zsh"])
    monkeypatch.setitem(sys.modules, "nira.ops.apply", mod)

    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        line = app.query_one("#statusline").content
        assert "applied alpha" in line
        assert "fail: zsh" in line


async def test_apply_without_ops_module_degrades(app, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("nira.ops"):
            raise ImportError("no ops")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        line = app.query_one("#statusline").content
        assert "apply unavailable" in line


# ---------------------------------------------------------------- doctor


async def test_doctor_shows_checks(app, monkeypatch) -> None:
    import sys
    import types

    mod = types.ModuleType("nira.ops.doctor")
    mod.run_checks = lambda host: iter(["ssh: ok", "disk: 20% free"])
    monkeypatch.setitem(sys.modules, "nira.ops.doctor", mod)

    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        table = app.query_one("#entries")
        assert table.row_count == 2
        line = app.query_one("#statusline").content
        assert "doctor alpha" in line
        assert app._doctor_stream == ["ssh: ok", "disk: 20% free"]


async def test_doctor_without_ops_module_degrades(app, monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("nira.ops"):
            raise ImportError("no ops")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        line = app.query_one("#statusline").content
        assert "doctor unavailable" in line


async def test_doctor_then_refresh_returns_to_grid(app, monkeypatch) -> None:
    import sys
    import types

    mod = types.ModuleType("nira.ops.doctor")
    mod.run_checks = lambda host: iter(["ssh: ok"])
    monkeypatch.setitem(sys.modules, "nira.ops.doctor", mod)

    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        assert app._mode == "doctor"
        await pilot.press("r")
        await pilot.pause()
        assert app._mode == "grid"
        # grid restored with entries preview
        assert app.query_one("#entries").row_count == 3


# ---------------------------------------------------------------- keys


async def test_quit_key(app) -> None:
    async with app.run_test() as pilot:
        await pilot.press("q")
        # app exits cleanly when context manager closes


def test_color_map_complete() -> None:
    assert STATUS_COLORS == {
        "ok": "green",
        "drifted": "yellow",
        "missing": "red",
        "protected": "magenta",
        "skipped": "dim",
    }
