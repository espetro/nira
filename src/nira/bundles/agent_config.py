"""agent-config: AGENTS.md/TOOLING.md/RTK.md verbatim, REMOTE.md templated."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig
from pathlib import Path

from nira.core.bundle import Bundle, Requirement, register

GLOBAL_DIR = Path.home() / ".config" / "agent"
VERBATIM = ("AGENTS.md", "TOOLING.md", "RTK.md")
TEMPLATED = {"REMOTE.md": {"template": "REMOTE.md.j2"}}


@register
class AgentConfigBundle(Bundle):
    name = "agent-config"
    description = "Global agent instruction files"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        for f in VERBATIM:
            yield Requirement(
                name=str(GLOBAL_DIR / f), kind="file", params={"verbatim": True}
            )
        for f, params in TEMPLATED.items():
            yield Requirement(
                name=str(GLOBAL_DIR / f), kind="file", params={"template": params["template"]}
            )
