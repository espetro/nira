"""agent-runtimes: claude, crush, maki, uv, pnpm, node, bun via mise."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

MISE_TOOLS = ("claude", "crush", "maki", "uv", "pnpm", "node", "bun")


@register
class AgentRuntimesBundle(Bundle):
    name = "agent-runtimes"
    description = "Agent CLIs and runtimes via mise"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield pkg("mise")  # precondition
        for tool in MISE_TOOLS:
            yield Requirement(
                name=f"mise:{tool}", kind="custom", params={"mise": True, "tool": tool}
            )
