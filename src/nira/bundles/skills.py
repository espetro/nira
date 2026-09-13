"""skills: asm + agent-skills repo relink."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundle_paths import home as host_home
from nira.core.bundle import Bundle, Requirement, register

AGENT_SKILLS_REPO = "https://github.com/josocjoq/agent-skills"


@register
class SkillsBundle(Bundle):
    name = "skills"
    description = "skill asm and agent-skills checkout relinked into ~/.claude/skills"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        home = host_home(host)
        yield Requirement(
            name=f"{home}/agent-skills",
            kind="directory",
            params={"repo": AGENT_SKILLS_REPO},
        )
        yield Requirement(
            name=f"{home}/.claude/skills",
            kind="custom",
            params={"relink_to": f"{home}/agent-skills"},
        )
