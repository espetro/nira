"""agent-runtimes: claude, crush, maki, uv, pnpm, node, bun via mise."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

# name -> mise spec. Registry shortnames resolve via `mise registry`; maki is not in the
# registry, so it uses its explicit github backend (tontinton/maki, release tarballs).
MISE_TOOLS = {
    "claude": "claude",
    "crush": "crush",
    "maki": "github:tontinton/maki",
    "uv": "uv",
    "pnpm": "pnpm",
    "node": "node",
    "bun": "bun",
}


@register
class AgentRuntimesBundle(Bundle):
    name = "agent-runtimes"
    description = "Agent CLIs and runtimes via mise"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield pkg("mise")  # precondition
        for name, spec in MISE_TOOLS.items():
            params = {"mise": True, "tool": spec, "bin": name, "scope": "agent"}
            yield Requirement(name=f"mise:{name}", kind="custom", params=params)
