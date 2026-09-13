"""Bundle interface.

A bundle is a unit of fleet configuration (tools, services, secrets, files).
Bundles declare REQUIREMENTS (what must exist); planning computes facts and
diffs requirements against reality. Apply executes only the gap.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nira.core.model import HostConfig, Plan, PlanEntry


@dataclass
class Requirement:
    """A single required component on a host."""

    name: str
    kind: str  # "package" | "file" | "service" | "secret" | "directory" | "custom"
    params: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}


class Bundle(ABC):
    """Base class for all bundles. Subclasses register via entry points or a registry."""

    name: str = "abstract"
    description: str = ""
    os_support: tuple[str, ...] = ("macos", "linux")
    priority: int = 50  # lower applies first (secret-broker = 0)

    def supports(self, host: "HostConfig") -> bool:
        return host.os in self.os_support

    @abstractmethod
    def requirements(self, host: "HostConfig") -> Iterator[Requirement]:
        """Yield requirements for this host (may vary by host)."""

    def plan(self, host: "HostConfig", facts: dict[str, Any]) -> "Iterator[PlanEntry]":
        """Default planner: requirement satisfied? -> OK/MISSING/DRIFTED."""
        from nira.core.model import Op, PlanEntry, Status

        for req in self.requirements(host):
            yield PlanEntry(
                bundle=self.name,
                component=req.name,
                op=Op.INSTALL,
                status=self._assess(req, facts),
                detail="",
            )

    def _assess(self, req: Requirement, facts: dict[str, Any]) -> "Status":
        from nira.core.model import Status

        found = facts.get(req.kind, {}).get(req.name)
        if found is None:
            return Status.MISSING
        if found is True:
            return Status.OK
        return Status.DRIFTED


REGISTRY: dict[str, type[Bundle]] = {}


def register(cls: type[Bundle]) -> type[Bundle]:
    REGISTRY[cls.name] = cls
    return cls


def get_bundle(name: str) -> Bundle:
    if name not in REGISTRY:
        # ensure builtins are imported
        import nira.bundles  # noqa: F401,PLC0415
    return REGISTRY[name]()
