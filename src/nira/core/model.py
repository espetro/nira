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
    kind: str | None = None  # requirement kind for the executor (package/file/...)
    params: dict[str, Any] = field(default_factory=dict)

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
    fleet_path: str | None = None  # set by CLI for apply-time host resolution
    protected_patterns: tuple[str, ...] = ()  # host protected globs, honored at apply

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
    agent_user: str | None = None  # run-as user for agent-scope work
    fleet_path: str | None = None  # fleet repo root, set by load_hosts when known
    overrides: dict[str, Any] = field(default_factory=dict)  # this host's overrides.yaml entry

    def override_path(self, key: str) -> Any:
        """Override ``key`` as a HostPath if it is a ``{home: rel}`` / ``{abs: path}`` mapping.

        Returns the raw value otherwise (str, list, None...)."""
        from nira.core.paths import HostPath

        val = self.overrides.get(key)
        if isinstance(val, dict) and len(val) == 1:
            if "home" in val:
                return HostPath.home(str(val["home"]))
            if "abs" in val:
                return HostPath.abs(str(val["abs"]))
        return val

    def resolve_override(
        self, key: str, scope: str = "agent", default: Any = None, home_of: Any = None
    ) -> Any:
        """Override value with ``{home: rel}`` rendered for the ``scope`` run-as user's home.

        ``home_of`` (user -> home) is injectable; the default resolves on the host.
        Non-path values come back unchanged; a missing key returns ``default``
        (itself rendered if it is a HostPath)."""
        from nira.core.paths import HostPath
        from nira.core.render_tpl import HostCtx

        val = self.override_path(key) if key in self.overrides else default
        if isinstance(val, HostPath):
            return val.render(HostCtx.for_host(self, scope, home_of=home_of).home)
        return val

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
            agent_user=data.get("agent_user"),
            fleet_path=data.get("fleet_path"),
            overrides=dict(data.get("overrides") or {}),
        )


SCOPES = ("agent", "admin")


def run_as(host: HostConfig, scope: str = "admin") -> str:
    """User an operation of the given scope runs as on ``host``."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if scope == "agent" and host.agent_user:
        return host.agent_user
    return host.ssh_user


def load_manifest(path: Path) -> dict[str, Any]:
    """Load the fleet manifest (YAML)."""
    return yaml.safe_load(path.read_text())


def load_overrides(fleet_path: Path) -> dict[str, dict[str, Any]]:
    """Per-host overrides from <fleet>/group_data/overrides.yaml ({} if absent)."""
    f = Path(fleet_path) / "group_data" / "overrides.yaml"
    if not f.exists():
        return {}
    data = yaml.safe_load(f.read_text()) or {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def load_hosts(
    inventory_dir: Path,
    overrides: dict[str, dict[str, Any]] | None = None,
    fleet_path: Path | None = None,
) -> list[HostConfig]:
    """Load hosts; inventory keys win, overrides fill ``agent_user`` gaps.

    If ``overrides`` is None and ``inventory_dir`` is ``<fleet>/inventory``,
    the fleet's group_data/overrides.yaml is picked up automatically. Each host
    carries its override dict (``overrides``) and ``fleet_path`` (given, or the
    parent of a directory named ``inventory``)."""
    inventory_dir = Path(inventory_dir)
    if fleet_path is None and inventory_dir.name == "inventory":
        fleet_path = inventory_dir.parent
    if overrides is None:
        overrides = load_overrides(fleet_path or inventory_dir.parent)
    hosts = []
    for f in sorted(inventory_dir.glob("*.py")):
        ns: dict[str, Any] = {}
        exec(compile(f.read_text(), str(f), "exec"), ns)  # noqa: S102 - trusted fleet repo
        for data in ns.get("HOSTS", []):
            data = dict(data)
            if data.get("agent_user") is None:
                extra = overrides.get(data.get("name"), {}).get("agent_user")
                if extra:
                    data["agent_user"] = extra
            data.setdefault("fleet_path", str(fleet_path) if fleet_path else None)
            data.setdefault("overrides", overrides.get(data.get("name"), {}))
            hosts.append(HostConfig.from_dict(data))
    return hosts
