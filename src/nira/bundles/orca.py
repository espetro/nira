"""orca: orca/orcad binary + pairing-state check (config presence only)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.core.bundle import Bundle, Requirement, register


@register
class OrcaBundle(Bundle):
    name = "orca"
    description = "orca/orcad binaries and pairing-state presence check"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(name="orca", kind="package")
        yield Requirement(name="orcad", kind="package")
        # pairing state is verify-only: config presence, never mutate
        yield Requirement(
            name="orca-pairing", kind="custom", params={"check": "config-presence-only"}
        )
