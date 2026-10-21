"""Fleet context on HostConfig: fleet_path, overrides, resolve_override."""

from __future__ import annotations

from pathlib import Path

from nira.core.model import HostConfig, load_hosts
from nira.core.paths import HostPath

OVERRIDES = """
ovhcloud:
  agent_user: own-agents
  agent_user_age_key: {home: .config/age/own-agents.key}
  agent_skills_dir: {home: .agents/skills}
  mcp_enable: [Stripe]
  plain: /srv/x
"""


def _fleet(tmp_path: Path) -> Path:
    (tmp_path / "inventory").mkdir()
    (tmp_path / "group_data").mkdir()
    (tmp_path / "inventory" / "hosts.py").write_text(
        "HOSTS=[dict(name='ovhcloud',ssh_user='debian',address='x',os='linux'),"
        "dict(name='mac',ssh_user='josocjoq',address='y',os='macos')]\n"
    )
    (tmp_path / "group_data" / "overrides.yaml").write_text(OVERRIDES)
    return tmp_path


def test_load_hosts_sets_fleet_path_and_overrides(tmp_path: Path) -> None:
    fleet = _fleet(tmp_path)
    hosts = {h.name: h for h in load_hosts(fleet / "inventory")}
    assert hosts["ovhcloud"].fleet_path == str(fleet)
    assert hosts["ovhcloud"].overrides["mcp_enable"] == ["Stripe"]
    assert hosts["mac"].overrides == {}


def test_resolve_override_uses_agent_user_home(tmp_path: Path) -> None:
    h = {x.name: x for x in load_hosts(_fleet(tmp_path) / "inventory")}["ovhcloud"]
    homes = {"own-agents": "/home/own-agents", "debian": "/home/debian"}
    got = h.resolve_override("agent_user_age_key", "agent", home_of=homes.__getitem__)
    assert got == "/home/own-agents/.config/age/own-agents.key"
    # admin scope resolves for the ssh user instead
    got = h.resolve_override("agent_skills_dir", "admin", home_of=homes.__getitem__)
    assert got == "/home/debian/.agents/skills"


def test_resolve_override_plain_missing_and_default() -> None:
    h = HostConfig("m", "josocjoq", "y", "macos", overrides={"p": "/srv/x", "l": ["a"]})
    homes = {"josocjoq": "/Users/josocjoq"}.__getitem__
    assert h.resolve_override("p", "agent", home_of=homes) == "/srv/x"
    assert h.resolve_override("l", home_of=homes) == ["a"]
    assert h.resolve_override("nope", home_of=homes) is None
    got = h.resolve_override("nope", "agent", HostPath.home(".agents/skills"), homes)
    assert got == "/Users/josocjoq/.agents/skills"  # mac: no agent_user -> ssh user


def test_resolve_override_default_home_lookup_is_hermetic(tmp_path: Path) -> None:
    h = {x.name: x for x in load_hosts(_fleet(tmp_path) / "inventory")}["ovhcloud"]
    # conftest patches the home lookup: /home/<run-as user>
    assert h.resolve_override("agent_skills_dir") == "/home/own-agents/.agents/skills"
