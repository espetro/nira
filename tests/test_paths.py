"""HostPath, home resolution, run_as, and the no-hardcoded-home lint."""

from __future__ import annotations

from pathlib import Path

import pytest

import nira.bundles  # noqa: F401
from nira.core.bundle import REGISTRY
from nira.core.model import HostConfig, load_hosts, load_overrides, run_as
from nira.core.paths import (
    HostPath,
    HostPathError,
    parse_dscl,
    parse_getent,
    resolve_home,
)
from nira.ops.inventory import host_inventory

FORBIDDEN = ("/Users/", "/home/", "~/", "$HOME", "__HOME__")
SRC = Path(__file__).resolve().parent.parent / "src" / "nira"


def lint_dir(root: Path) -> list[str]:
    """Return 'file: token' for each forbidden token in any template under root."""
    bad = []
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.suffix in {".j2", ".tmpl", ".tpl", ".yaml", ".yml", ".sh", ".md"}:
            text = f.read_text()
            bad += [f"{f}: {t}" for t in FORBIDDEN if t in text]
    return bad


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, list | tuple):
        for v in obj:
            yield from strings(v)


def h(os_name="linux", **kw) -> HostConfig:
    return HostConfig(name="h", ssh_user="debian", address="a", os=os_name, **kw)


def test_home_and_abs() -> None:
    assert HostPath.home(".config/x").render("/home/own-agents") == "/home/own-agents/.config/x"
    assert HostPath.home("").render("/Users/z") == "/Users/z"
    assert HostPath.abs("/etc/systemd/system/a.service").render("/h") == (
        "/etc/systemd/system/a.service"
    )
    assert str(HostPath.home("a/b")) == "{{ home }}/a/b"
    assert str(HostPath.home("a") / "b") == "{{ home }}/a/b"


@pytest.mark.parametrize(
    "bad", ["/Users/josocjoq/x", "/home/debian", "/home", "~/x", "/a/$HOME/b", "/a/${X}", "rel/x"]
)
def test_abs_rejects(bad: str) -> None:
    with pytest.raises(HostPathError):
        HostPath.abs(bad)


@pytest.mark.parametrize("bad", ["/abs", "~/x", "$HOME/x", "${H}/x", "../x", "a/../../x"])
def test_home_rejects(bad: str) -> None:
    with pytest.raises(HostPathError):
        HostPath.home(bad)


def test_direct_construction_rejected() -> None:
    with pytest.raises(HostPathError):
        HostPath("weird", "/x")


GETENT = "own-agents:x:1001:1001:agent:/home/own-agents:/bin/bash\n"
DSCL = "NFSHomeDirectory: /Users/josocjoq\n"


def test_parsers() -> None:
    assert parse_getent(GETENT, "own-agents") == "/home/own-agents"
    assert parse_dscl(DSCL, "josocjoq") == "/Users/josocjoq"
    with pytest.raises(HostPathError):
        parse_getent("", "x")
    with pytest.raises(HostPathError):
        parse_getent(GETENT, "other")
    with pytest.raises(HostPathError):
        parse_dscl("nope", "x")


def test_resolve_cross_os_cross_user() -> None:
    calls: list[str] = []

    def run_linux(cmd: str) -> str:
        calls.append(cmd)
        return GETENT

    assert resolve_home("linux", "own-agents", run_linux) == "/home/own-agents"
    assert calls == ["getent passwd own-agents"]
    mac: list[str] = []

    def run_mac(cmd: str) -> str:
        mac.append(cmd)
        return DSCL

    assert resolve_home("macos", "josocjoq", run_mac) == "/Users/josocjoq"
    assert mac == ["dscl . -read /Users/josocjoq NFSHomeDirectory"]


def test_run_as() -> None:
    ovh = h(agent_user="own-agents")
    assert run_as(ovh, "agent") == "own-agents"
    assert run_as(ovh, "admin") == "debian"
    assert run_as(h(), "agent") == "debian"
    with pytest.raises(ValueError):
        run_as(h(), "root")


def test_agent_user_inventory_and_overrides(tmp_path: Path) -> None:
    fleet = tmp_path
    (fleet / "inventory").mkdir()
    (fleet / "group_data").mkdir()
    (fleet / "inventory" / "hosts.py").write_text(
        "HOSTS=[dict(name='ovhcloud',ssh_user='debian',address='x',os='linux'),"
        "dict(name='m',ssh_user='u',address='y',os='macos',agent_user='bot')]\n"
    )
    (fleet / "group_data" / "overrides.yaml").write_text(
        "ovhcloud:\n  agent_user: own-agents\nm:\n  agent_user: ignored\n"
    )
    assert load_overrides(fleet)["ovhcloud"]["agent_user"] == "own-agents"
    hosts = {x.name: x for x in load_hosts(fleet / "inventory")}
    assert hosts["ovhcloud"].agent_user == "own-agents"
    assert hosts["m"].agent_user == "bot"
    assert host_inventory(hosts["ovhcloud"])[1]["agent_user"] == "own-agents"
    assert load_overrides(tmp_path / "nope") == {}


# ---------------- lint ----------------


def test_lint_catches_literals(tmp_path: Path) -> None:
    for i, tok in enumerate(FORBIDDEN):
        (tmp_path / f"t{i}.j2").write_text(f"a {tok} b")
    (tmp_path / "ok.j2").write_text("{{ home }}/.config")
    assert len(lint_dir(tmp_path)) == len(FORBIDDEN)


def test_repo_templates_clean() -> None:
    assert lint_dir(SRC) == []


@pytest.mark.parametrize("os_name", ["macos", "linux"])
def test_bundle_requirements_have_no_literals(os_name: str) -> None:
    host = h(os_name, agent_user="own-agents")
    for cls in REGISTRY.values():
        for req in cls().requirements(host):
            for s in strings([req.name, req.params]):
                for tok in FORBIDDEN:
                    assert tok not in s, (cls.name, req.name, tok)


def test_no_user_literal_in_source() -> None:
    for f in SRC.rglob("*.py"):
        assert "/Users/josocjoq" not in f.read_text(), f


def test_home_symbolic() -> None:
    from nira.bundle_paths import home

    assert home(h()) == "{{ home }}"
