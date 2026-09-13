"""agent-config: AGENTS.md/TOOLING.md/RTK.md verbatim, REMOTE.md templated."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig
from nira.bundle_paths import home as host_home
from nira.core.bundle import Bundle, Requirement, register

VERBATIM = ("AGENTS.md", "TOOLING.md", "RTK.md")
TEMPLATED = {"REMOTE.md": {"template": "REMOTE.md.j2"}}


@register
class AgentConfigBundle(Bundle):
    name = "agent-config"
    description = "Global agent instruction files"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        global_dir = f"{host_home(host)}/.config/agent"
        for f in VERBATIM:
            yield Requirement(
                name=f"{global_dir}/{f}", kind="file", params={"verbatim": True}
            )
        for f, params in TEMPLATED.items():
            yield Requirement(
                name=f"{global_dir}/{f}", kind="file", params={"template": params["template"]}
            )
