"""macos-desktop: desktop-level macOS settings and apps. Optional."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

CASKS = ("raycast", "ghostty", "wezterm")


@register
class MacosDesktopBundle(Bundle):
    name = "macos-desktop"
    description = "Desktop apps and settings"
    os_support: tuple[str, ...] = ("macos",)
    optional = True

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        for c in CASKS:
            yield pkg(f"cask:{c}")
        yield Requirement(
            name="macos-defaults", kind="custom", params={"type": "defaults-write-set"}
        )
