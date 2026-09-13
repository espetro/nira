"""core: brew/apt baseline, mise, git, nono."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

BASE = {
    "macos": ["mise", "git"],
    "linux": ["mise", "git"],
}


@register
class CoreBundle(Bundle):
    name = "core"
    description = "Baseline packages: mise, git, nono"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        for name in BASE.get(host.os, []):
            yield pkg(name)
        yield Requirement(name="nono", kind="custom", params={"check": "binary-in-path"})
