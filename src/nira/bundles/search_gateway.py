"""search-gateway: degoog systemd unit + degoog-mcp. Linux only, localhost bind."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.core.bundle import Bundle, Requirement, register

UNIT = "/etc/systemd/system/degoog.service"


@register
class SearchGatewayBundle(Bundle):
    name = "search-gateway"
    description = "degoog search gateway systemd unit + degoog-mcp"
    os_support: tuple[str, ...] = ("linux",)

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(name="degoog-mcp", kind="package")
        yield Requirement(
            name="degoog",
            kind="service",
            params={
                "unit_path": UNIT,
                "MemoryMax": "300M",
                "bind": "127.0.0.1",
            },
        )
