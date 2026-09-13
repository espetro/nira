"""skills: asm + agent-skills repo relink."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig
from pathlib import Path

from nira.core.bundle import Bundle, Requirement, register

AGENT_SKILLS_REPO = "https://github.com/josocjoq/agent-skills"


@register
class SkillsBundle(Bundle):
    name = "skills"
    description = "skill asm and agent-skills checkout relinked into ~/.claude/skills"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        home = Path.home()
        yield Requirement(
            name=str(home / "agent-skills"),
            kind="directory",
            params={"repo": AGENT_SKILLS_REPO},
        )
        yield Requirement(
            name=str(home / ".claude" / "skills"),
            kind="custom",
            params={"relink_to": str(home / "agent-skills")},
        )
