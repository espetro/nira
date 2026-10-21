"""Template/param rendering with StrictUndefined and per-run-as-user home."""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

import nira.bundles  # noqa: F401
from nira.core.bundle import REGISTRY
from nira.core.model import HostConfig
from nira.core.render_tpl import HostCtx, render_string, render_template, resolve_params

HOMES = {"debian": "/home/debian", "own-agents": "/home/own-agents", "josocjoq": "/Users/josocjoq"}

HOSTS = [
    HostConfig(name="ovhcloud", ssh_user="debian", address="a", os="linux", agent_user="own-agents"),
    HostConfig(name="mini", ssh_user="josocjoq", address="b", os="macos"),
    HostConfig(name="plain", ssh_user="debian", address="c", os="linux"),
]

TEMPLATES = {
    "a.j2": "home={{ home }} user={{ user }}\n",
    "b.sh.tpl": "#!/bin/sh\nexport X={{ home }}/.local\n",
}


@pytest.fixture
def tdir(tmp_path: Path) -> Path:
    for n, t in TEMPLATES.items():
        (tmp_path / n).write_text(t)
    return tmp_path


def ctx(host: HostConfig, scope: str, tdir: Path) -> HostCtx:
    return HostCtx.for_host(host, scope, home_of=HOMES.__getitem__, template_dirs=[tdir])


@pytest.mark.parametrize("host", HOSTS, ids=lambda x: x.name)
@pytest.mark.parametrize("scope", ["agent", "admin"])
def test_all_templates_all_hosts(host: HostConfig, scope: str, tdir: Path) -> None:
    c = ctx(host, scope, tdir)
    out = render_template("a.j2", c)
    assert out == f"home={HOMES[c.user]} user={c.user}\n"
    assert render_template("b.sh.tpl", c).count(HOMES[c.user]) == 1


def test_ovhcloud_agent_vs_admin(tdir: Path) -> None:
    assert "own-agents" in render_template("a.j2", ctx(HOSTS[0], "agent", tdir))
    assert "/home/debian" in render_template("a.j2", ctx(HOSTS[0], "admin", tdir))


def test_unresolved_var_raises(tdir: Path) -> None:
    (tdir / "bad.j2").write_text("{{ ssh_user }}")
    with pytest.raises(UndefinedError):
        render_template("bad.j2", ctx(HOSTS[0], "agent", tdir))
    with pytest.raises(UndefinedError):
        render_string("{{ HOME }}", ctx(HOSTS[0], "agent", tdir))


def test_resolve_params(tdir: Path) -> None:
    c = ctx(HOSTS[0], "agent", tdir)
    out = resolve_params({"a": "{{ home }}/x", "n": 3, "l": ["{{ user }}", "plain"]}, c)
    assert out == {"a": "/home/own-agents/x", "n": 3, "l": ["own-agents", "plain"]}


def test_bundle_names_render_for_every_host(tdir: Path) -> None:
    for host in HOSTS:
        for cls in REGISTRY.values():
            for req in cls().requirements(host):
                c = ctx(host, req.params.get("scope", "admin"), tdir)
                assert "{{" not in render_string(req.name, c)
                resolve_params(req.params, c)


def test_agent_scoped_bundle_paths_use_agent_home(tdir: Path) -> None:
    reqs = list(REGISTRY["bifrost-stack"]().requirements(HOSTS[0]))
    cfg = next(r for r in reqs if r.name.endswith("config.json"))
    assert cfg.params["scope"] == "agent"
    c = ctx(HOSTS[0], "agent", tdir)
    assert render_string(cfg.name, c) == "/home/own-agents/.config/bifrost/config.json"


def test_planner_renders_home_for_run_as_user(monkeypatch) -> None:
    """Plan entries carry resolved paths for the agent user, never `{{ home }}` literals."""
    from nira.core import planner
    from nira.core.model import HostConfig

    h = HostConfig("ovh", "debian", "a", "linux", bundles=["skills"], agent_user="own-agents")
    monkeypatch.setattr(planner, "collect_facts", lambda host, reqs=None: {})
    plan = planner.build_plan(h)
    names = [e.component for e in plan.entries]
    assert not any("{{" in n for n in names)
    assert any(n.startswith("/home/own-agents/") for n in names)
