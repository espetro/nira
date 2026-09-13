"""nira core data model.

Manifest-first, gap-filling-only semantics live here. Every operation type
knows how to (a) describe its requirement, (b) check current state, and (c)
emit a plan entry. Actual host mutation is delegated to the pyinfra ops layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml


class Op(StrEnum):
    INSTALL = "install"
    CONFIG = "config"
    SERVICE = "service"
    SECRET = "secret"
    VERIFY = "verify"
    SKIP = "skip"


class Status(StrEnum):
    MISSING = "missing"  # required by manifest, absent on host -> will install
    DRIFTED = "drifted"  # exists but does not satisfy manifest -> report (fix only if not protected)
    OK = "ok"  # exists and satisfies manifest -> never modify
    PROTECTED = "protected"  # on protected list -> report-only, always
    SKIPPED = "skipped"  # bundle not selected for this host


@dataclass
class PlanEntry:
    bundle: str
    component: str
    op: Op
    status: Status
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle": self.bundle,
            "component": self.component,
            "op": str(self.op),
            "status": str(self.status),
            "detail": self.detail,
        }


@dataclass
class Plan:
    host: str
    entries: list[PlanEntry] = field(default_factory=list)

    def add(self, entry: PlanEntry) -> None:
        self.entries.append(entry)

    @property
    def changes(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.status in (Status.MISSING, Status.DRIFTED)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "changes": len(self.changes),
            "entries": [e.to_dict() for e in self.entries],
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), indent=2)


@dataclass
class HostConfig:
    """One target host from the inventory."""

    name: str
    ssh_user: str
    address: str
    os: str  # "macos" | "linux"
    bundles: list[str] = field(default_factory=list)
    protected_components: list[str] = field(default_factory=list)
    local: bool = False  # @local operation (kinosmac itself)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HostConfig:
        return cls(
            name=data["name"],
            ssh_user=data["ssh_user"],
            address=data["address"],
            os=data["os"],
            bundles=data.get("bundles", []),
            protected_components=data.get("protected_components", []),
            local=data.get("local", False),
        )


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the fleet manifest (YAML)."""
    return yaml.safe_load(path.read_text())


def load_hosts(inventory_dir: Path) -> list[HostConfig]:
    hosts = []
    for f in sorted(inventory_dir.glob("*.py")):
        ns: dict[str, Any] = {}
        exec(compile(f.read_text(), str(f), "exec"), ns)  # noqa: S102 - trusted fleet repo
        for data in ns.get("HOSTS", []):
            hosts.append(HostConfig.from_dict(data))
    return hosts
