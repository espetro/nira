"""media-audio: macOS audio tooling. Optional."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

PACKAGES = ("ffmpeg", "sox", "yt-dlp")


@register
class MediaAudioBundle(Bundle):
    name = "media-audio"
    description = "Audio/media processing tools"
    os_support: tuple[str, ...] = ("macos",)
    optional = True

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        for p in PACKAGES:
            yield pkg(p)
