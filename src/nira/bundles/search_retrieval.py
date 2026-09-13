"""search-retrieval: rg, ugrep, fd, fzf, jq, ast-grep, ghgrab, repomix, opensrc, gh, ghx.

Linux/VPS per manifest; macos only via explicit host bundle selection override.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundles.common import pkg
from nira.core.bundle import Bundle, Requirement, register

TOOLS = (
    "ripgrep",
    "ugrep",
    "fd",
    "fzf",
    "jq",
    "ast-grep",
    "ghgrab",
    "repomix",
    "opensrc",
    "gh",
    "ghx",
)


@register
class SearchRetrievalBundle(Bundle):
    name = "search-retrieval"
    description = "Search and code-retrieval tools"
    os_support: tuple[str, ...] = ("linux",)  # macos via host override below

    def supports(self, host: HostConfig) -> bool:
        # manifest lists bundle explicitly on macos hosts -> allow override
        return host.os == "linux" or "search-retrieval" in host.bundles

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        for t in TOOLS:
            yield pkg(t)
