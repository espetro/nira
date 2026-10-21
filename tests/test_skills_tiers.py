"""Skill tiers: core / cli:<tool> / project:<name>, delivered to the agent user's skills dir."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nira.bundles.skills import SkillsBundle, discover, parse_skill_md
from nira.core.bundle import RequirementError
from nira.core.model import HostConfig, Status
from nira.ops import executors
from nira.ops.pyinfra_facts import collect_via_pyinfra


def skill(root: Path, name: str, front: str) -> None:
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n{front}---\n\nbody\n")


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    skill(tmp_path, "writing", "tier: core\n")
    skill(tmp_path, "gh-triage", "tier: cli\nrequires: [gh]\n")
    skill(tmp_path, "needs-nothing-here", "tier: cli\nrequires: [definitely-not-a-bin-xyz]\n")
    skill(tmp_path, "brioso-deploy", "tier: project\nproject: brioso\n")
    skill(tmp_path, "other-proj", "tier: project\nproject: other\n")
    return tmp_path


def ovh(fleet: Path, **ov: object) -> HostConfig:
    return HostConfig(
        "ovhcloud", "debian", "x", "linux", bundles=["skills"], agent_user="own-agents",
        fleet_path=str(fleet),
        overrides={"agent_skills_dir": {"home": ".agents/skills"}, **ov},
    )


def entries(host: HostConfig, facts: dict) -> dict:
    return {e.component: e for e in SkillsBundle().plan(host, facts)}


SD = "/home/own-agents/.agents/skills"


def test_frontmatter_parsing_and_validation(tmp_path: Path) -> None:
    skill(tmp_path, "a", "tier: cli\nrequires: [x, y]\n")
    s = parse_skill_md(tmp_path / "skills" / "a" / "SKILL.md")
    assert (s.tier, s.requires) == ("cli", ("x", "y"))
    for bad in ("tier: cli\n", "tier: project\n", "tier: weird\n", ""):
        skill(tmp_path, "bad", bad)
        with pytest.raises(RequirementError):
            parse_skill_md(tmp_path / "skills" / "bad" / "SKILL.md")
        (tmp_path / "skills" / "bad" / "SKILL.md").unlink()
        (tmp_path / "skills" / "bad").rmdir()


def test_core_always_cli_gated_project_opt_in(fleet: Path) -> None:
    host = ovh(fleet, skill_projects=["brioso"])
    facts = {"custom": {"gh": True, "definitely-not-a-bin-xyz": False}}
    e = entries(host, facts)
    assert e[f"{SD}/writing"].status == Status.MISSING
    assert e[f"{SD}/gh-triage"].status == Status.MISSING  # gh present
    assert e[f"{SD}/needs-nothing-here"].status == Status.SKIPPED
    assert "definitely-not-a-bin-xyz" in e[f"{SD}/needs-nothing-here"].detail
    assert e[f"{SD}/brioso-deploy"].status == Status.MISSING  # opted in
    assert e["skill:other-proj"].status == Status.SKIPPED  # not opted in
    assert "other" in e["skill:other-proj"].detail
    # binary probes are facts, not changes
    assert "gh" not in e and not any(c.endswith("definitely-not-a-bin-xyz") for c in e)


def test_projects_default_off(fleet: Path) -> None:
    e = entries(ovh(fleet), {"custom": {"gh": True}})
    assert "skill:brioso-deploy" in e and e["skill:brioso-deploy"].status == Status.SKIPPED
    assert f"{SD}/brioso-deploy" not in e


def test_delivered_to_agent_user_skills_dir_not_ssh_user(fleet: Path) -> None:
    reqs = list(SkillsBundle().resolved(ovh(fleet)))
    paths = [r.name for r in reqs if r.kind == "custom" and "relink_to" in r.params]
    assert paths and all(p.startswith(SD + "/") for p in paths)
    assert not any("/home/debian" in r.name for r in reqs)
    assert all(r.params["scope"] == "agent" for r in reqs)
    clone = next(r for r in reqs if r.kind == "directory")
    assert clone.name == "/home/own-agents/agent-skills"


def test_skills_dir_defaults_and_mac_host(fleet: Path) -> None:
    mac = HostConfig("mac", "josocjoq", "y", "macos", fleet_path=str(fleet))
    names = {r.name for r in SkillsBundle().resolved(mac)}
    assert "/home/josocjoq/.agents/skills/writing" in names  # conftest home lookup
    custom = HostConfig(
        "m", "u", "y", "macos", fleet_path=str(fleet),
        overrides={"agent_skills_dir": {"home": ".claude/skills"}},
    )
    assert any(n.endswith("/.claude/skills/writing") for n in
               {r.name for r in SkillsBundle().resolved(custom)})


def test_seed_only_skills_dir_falls_back_to_whole_checkout(tmp_path: Path) -> None:
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "SEED.md").write_text("seed")
    (tmp_path / "skills" / "README.md").write_text("tiers")
    host = ovh(tmp_path)
    assert discover(host) == []
    reqs = list(SkillsBundle().resolved(host))
    relink = [r for r in reqs if r.kind == "custom"]
    assert [(r.name, r.params["relink_to"]) for r in relink] == [
        (SD, "/home/own-agents/agent-skills")
    ]


def test_skills_source_override(fleet: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    other = tmp_path_factory.mktemp("repo")
    d = other / "solo"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: solo\ntier: core\n---\n")
    host = ovh(fleet, skills_source=str(other))
    assert [s.name for s in discover(host)] == ["solo"]


def test_cli_probe_runs_for_real_bins_only(fleet: Path) -> None:
    """Facts via the real probe path (local bash): present bin -> installed, absent -> skipped."""
    skill(fleet, "uses-sh", "tier: cli\nrequires: [sh]\n")
    host = HostConfig(
        "t", "tester", "localhost", "linux", local=True, fleet_path=str(fleet), overrides={}
    )
    b = SkillsBundle()
    facts = collect_via_pyinfra(host, list(b.resolved(host)))
    assert facts["custom"]["sh"] is True
    assert facts["custom"]["definitely-not-a-bin-xyz"] is False
    e = entries_of(b, host, facts)
    assert e["uses-sh"] == Status.MISSING or e["uses-sh"] == Status.DRIFTED
    assert e["needs-nothing-here"] == Status.SKIPPED


def entries_of(b: SkillsBundle, host: HostConfig, facts: dict) -> dict:
    return {Path(e.component).name: e.status for e in b.plan(host, facts)}


def test_relink_per_skill_skips_dotfiles_and_keeps_real_files(tmp_path: Path) -> None:
    repo = tmp_path / "agent-skills" / "writing"
    (repo / ".git").mkdir(parents=True)
    (repo / "SKILL.md").write_text("x")
    (repo / "notes.md").write_text("y")
    dest = tmp_path / "skills" / "writing"
    dest.mkdir(parents=True)
    (dest / "notes.md").write_text("mine")  # real file must survive
    ctx = executors.ExecCtx(os="linux", ssh_user="tester", local=True)
    built = executors.build_ops(
        "custom", str(dest), {"relink_to": str(repo), "scope": "agent"}, ctx
    )
    script = built.specs[0].kwargs["commands"][0]
    p = subprocess.run(script, shell=True, capture_output=True, text=True, check=False)
    assert p.returncode == 0 and executors.CHANGED_MARK in p.stdout
    assert (dest / "SKILL.md").is_symlink()
    assert not (dest / ".git").exists()
    assert (dest / "notes.md").read_text() == "mine" and not (dest / "notes.md").is_symlink()
