"""services-mini: agents user, LaunchDaemon pattern, mcp-sim. macOS only."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.core.bundle import Bundle, Requirement, register

LAUNCHD_LABEL = "io.nira.mcp-sim"
PLIST = "/Library/LaunchDaemons/io.nira.mcp-sim.plist"


@register
class ServicesMiniBundle(Bundle):
    name = "services-mini"
    description = "agents user, LaunchDaemon pattern, mcp-sim"
    os_support: tuple[str, ...] = ("macos",)

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(name="own-agents", kind="custom", params={"type": "user"})
        yield Requirement(
            name="mcp-sim",
            kind="service",
            params={
                "label": LAUNCHD_LABEL,
                "plist_path": PLIST,
                "bootstrap": True,  # launchctl bootstrap, never legacy load
            },
        )
