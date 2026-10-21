"""skills: agent-skills checkout, delivered per tier into the agent user's skills dir.

Tiers come from each skill's ``SKILL.md`` frontmatter (see fleet ``skills/README.md``):

- ``core``: always installed.
- ``cli``: installed only when every ``requires`` binary resolves on the host
  (``command -v`` as the agent user, probed in the same fact pass).
- ``project``: opt-in per host via overrides ``skill_projects: [name, ...]``.

Metadata is read from the host override ``skills_source`` (a local checkout of the
skills repo; relative paths are relative to the fleet) or else ``<fleet>/skills``.
When no ``SKILL.md`` is found there (the fleet only holds SEED.md/README.md), all
skills of the target checkout are linked as ``core`` (legacy behaviour).

Delivery goes to the agent user's ``agent_skills_dir`` override (default
``{home: .agents/skills}``, resolved for the run-as user, never ssh_user). Each
selected skill is a real directory whose top-level entries are symlinks into the
checkout, so tiers are per skill; dotfiles (``.git``) are never linked and real
files are never clobbered (executor relink).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from nira.bundle_paths import hp
from nira.core.bundle import Bundle, Requirement, RequirementError, register
from nira.core.paths import HostPath

if TYPE_CHECKING:
    from nira.core.model import HostConfig, PlanEntry

AGENT_SKILLS_REPO = "https://github.com/josocjoq/agent-skills"
CHECKOUT = "agent-skills"
DEFAULT_SKILLS_DIR = ".agents/skills"
TIERS = ("core", "cli", "project")
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    tier: str
    requires: tuple[str, ...] = ()
    project: str | None = None


def parse_skill_md(path: Path) -> Skill:
    m = _FRONT.match(path.read_text())
    meta = yaml.safe_load(m.group(1)) if m else None
    if not isinstance(meta, dict):
        raise RequirementError(f"{path}: SKILL.md has no frontmatter")
    name = str(meta.get("name") or path.parent.name)
    tier = meta.get("tier")
    if tier not in TIERS:
        raise RequirementError(f"{path}: tier must be one of {TIERS}, got {tier!r}")
    requires = tuple(str(b) for b in meta.get("requires") or ())
    if tier == "cli" and not requires:
        raise RequirementError(f"{path}: cli skill needs a non-empty 'requires'")
    if tier == "project" and not meta.get("project"):
        raise RequirementError(f"{path}: project skill needs 'project'")
    return Skill(name, tier, requires, str(meta["project"]) if meta.get("project") else None)


def discover(host: HostConfig) -> list[Skill]:
    """Skills with tier metadata (sorted by name); [] if the source has no SKILL.md."""
    src = host.overrides.get("skills_source")
    if src:
        root = Path(src)
        if not root.is_absolute() and host.fleet_path:
            root = Path(host.fleet_path) / root
    elif host.fleet_path:
        root = Path(host.fleet_path) / "skills"
    else:
        return []
    return [parse_skill_md(f) for f in sorted(root.glob("*/SKILL.md"))]


@register
class SkillsBundle(Bundle):
    name = "skills"
    description = "agent-skills checkout, tiered (core / cli:<tool> / project:<name>) into the agent skills dir"

    @staticmethod
    def skills_dir(host: HostConfig) -> str:
        val = host.override_path("agent_skills_dir")
        if isinstance(val, HostPath):
            return str(val)
        if isinstance(val, str) and val:
            return val
        return hp(DEFAULT_SKILLS_DIR)

    @staticmethod
    def _projects(host: HostConfig) -> set[str]:
        return {str(p) for p in host.overrides.get("skill_projects") or ()}

    def _split(self, host: HostConfig) -> tuple[list[Skill], list[Skill]]:
        """(selected, not-selected-by-host) skills; cli gating happens at plan time."""
        chosen, off = [], []
        for s in discover(host):
            if s.tier == "project" and s.project not in self._projects(host):
                off.append(s)
            else:
                chosen.append(s)
        return chosen, off

    def _relink(self, host: HostConfig, name: str | None) -> Requirement:
        dest = self.skills_dir(host)
        src = hp(CHECKOUT)
        if name:
            dest, src = f"{dest}/{name}", f"{src}/{name}"
        return Requirement(dest, "custom", {"relink_to": src, "scope": "agent"})

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(
            name=hp(CHECKOUT),
            kind="directory",
            params={"repo": AGENT_SKILLS_REPO, "scope": "agent"},
        )
        chosen, _ = self._split(host)
        if not chosen:
            yield self._relink(host, None)  # legacy: everything in the checkout
            return
        probed: set[str] = set()
        for s in chosen:
            yield self._relink(host, s.name)
            for b in s.requires:
                if b not in probed:  # probe-only: gates the cli skill at plan time
                    probed.add(b)
                    yield Requirement(b, "custom", {"check": "binary-in-path", "scope": "agent"})

    def plan(self, host: HostConfig, facts: dict[str, Any]) -> Iterator[PlanEntry]:
        from nira.core.model import Op, PlanEntry, Status

        chosen, off = self._split(host)
        by_name = {s.name: s for s in chosen}
        bins = {b for s in chosen for b in s.requires}
        for req in self.resolved(host):
            if req.kind == "custom" and req.params.get("check") and req.name in bins:
                continue  # binary probe is a fact, not a change
            skill = by_name.get(req.name.rsplit("/", 1)[-1]) if req.kind == "custom" else None
            if skill and skill.tier == "cli":
                missing = [b for b in skill.requires if facts.get("custom", {}).get(b) is not True]
                if missing:
                    yield PlanEntry(
                        bundle=self.name,
                        component=req.name,
                        op=Op.SKIP,
                        status=Status.SKIPPED,
                        detail=f"cli:{skill.name}: requires {', '.join(missing)} not on host",
                    )
                    continue
            yield from self._plan_one(host, req, facts)
        for s in off:
            yield PlanEntry(
                bundle=self.name,
                component=f"skill:{s.name}",
                op=Op.SKIP,
                status=Status.SKIPPED,
                detail=f"project:{s.project} not enabled (overrides skill_projects)",
            )

    def _plan_one(self, host: HostConfig, req: Requirement, facts: dict[str, Any]) -> Iterator[Any]:
        from nira.core.bundle import validate_requirement
        from nira.core.model import Op, PlanEntry

        validate_requirement(req, host.os)
        yield PlanEntry(
            bundle=self.name,
            component=req.name,
            op=Op.INSTALL,
            status=self._assess(req, facts),
            kind=req.kind,
            params=dict(req.params),
        )
