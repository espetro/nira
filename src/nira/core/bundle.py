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
    from nira.core.model import HostConfig, PlanEntry, Status


@dataclass
class Requirement:
    """A single required component on a host."""

    name: str
    kind: str  # "package" | "file" | "service" | "secret" | "directory" | "custom"
    params: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.params is None:
            self.params = {}


class RequirementError(ValueError):
    """A requirement is malformed (unknown kind/params, bad package name, ...)."""


SCOPES = ("agent", "admin")
_COMMON = {"scope"}

ACCEPTED_PARAMS: dict[str, frozenset[str]] = {
    "package": frozenset(_COMMON | {"bin", "verify_only"}),
    "file": frozenset(
        _COMMON | {"template", "content", "src", "verbatim", "env_ref", "mode", "sha256"}
    ),
    "directory": frozenset(_COMMON | {"repo"}),
    "secret": frozenset(_COMMON | {"sops_file", "key", "method", "consumer"}),
    "service": frozenset(
        _COMMON
        | {"unit_path", "unit_content", "adopt", "plist_path", "label", "bootstrap"}
        | {"MemoryMax", "bind", "url"}
    ),
    # bifrost admin API client (see nira.ops.mcp_admin)
    "mcp": frozenset(
        _COMMON
        | {"client", "enabled", "port", "creds_sops", "creds_file", "virtual_key"}
        | {"retries", "backoff"}
    ),
}

# custom requirements are a closed set of shapes, discriminated by one key
CUSTOM_VARIANTS: dict[str, frozenset[str]] = {
    "mise": frozenset(_COMMON | {"mise", "tool", "bin"}),
    "type": frozenset(_COMMON | {"type"}),
    "relink_to": frozenset(_COMMON | {"relink_to"}),
    "check": frozenset(_COMMON | {"check"}),
}
CUSTOM_TYPES = ("user", "defaults-write-set")
CUSTOM_CHECKS = ("binary-in-path", "config-presence-only")


def custom_variant(params: dict[str, Any]) -> str:
    for key in ("mise", "type", "relink_to", "check"):
        if key in params:
            return key
    raise RequirementError(
        f"custom requirement needs one of mise/type/relink_to/check; got {sorted(params)}"
    )


def default_scope(kind: str, params: dict[str, Any]) -> str:
    """Who runs the op: 'admin' (root via sudo) or 'agent' (the agent's own user)."""
    if "scope" in params:
        return params["scope"]
    if kind in ("package", "service"):
        return "admin"
    if kind == "custom" and params.get("type") == "user":
        return "admin"
    return "agent"


def validate_requirement(req: Requirement, os_name: str | None = None) -> None:
    """Reject unknown kinds/params and unresolvable package names (plan time)."""
    where = f"{req.kind} {req.name!r}"
    if req.kind == "custom":
        variant = custom_variant(req.params)
        allowed = CUSTOM_VARIANTS[variant]
        if variant == "type" and req.params["type"] not in CUSTOM_TYPES:
            raise RequirementError(f"{where}: unknown type {req.params['type']!r}")
        if variant == "check" and req.params["check"] not in CUSTOM_CHECKS:
            raise RequirementError(f"{where}: unknown check {req.params['check']!r}")
        if variant == "mise" and not req.params.get("tool"):
            raise RequirementError(f"{where}: mise requirement needs 'tool'")
        if variant == "mise":
            from nira.core.packages import PackageError, check_mise_spec

            try:
                check_mise_spec(str(req.params["tool"]))
            except PackageError as exc:
                raise RequirementError(f"{where}: {exc}") from exc
    elif req.kind in ACCEPTED_PARAMS:
        allowed = ACCEPTED_PARAMS[req.kind]
    else:
        raise RequirementError(f"{where}: unknown requirement kind {req.kind!r}")
    unknown = sorted(set(req.params) - allowed)
    if unknown:
        raise RequirementError(f"{where}: unknown params {unknown}; accepted: {sorted(allowed)}")
    scope = req.params.get("scope")
    if scope is not None and scope not in SCOPES:
        raise RequirementError(f"{where}: scope must be one of {SCOPES}, got {scope!r}")
    if req.kind == "package" and os_name:
        from nira.core.packages import PackageError, resolve_package

        try:
            resolve_package(req.name, os_name)
        except PackageError as exc:
            raise RequirementError(str(exc)) from exc


class Bundle(ABC):
    """Base class for all bundles. Subclasses register via entry points or a registry."""

    name: str = "abstract"
    description: str = ""
    os_support: tuple[str, ...] = ("macos", "linux")
    priority: int = 50  # lower applies first (secret-broker = 0)
    enabled: bool = True  # False: code kept, planner reports the bundle as skipped ("disabled")

    def supports(self, host: HostConfig) -> bool:
        return host.os in self.os_support

    @abstractmethod
    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        """Yield requirements for this host (may vary by host)."""

    def resolved(self, host: HostConfig) -> Iterator[Requirement]:
        """Requirements with ``{{ home }}`` / ``{{ user }}`` rendered for the run-as user.

        Home lookups are lazy: strings without template syntax never touch the host.
        """
        from nira.core.render_tpl import HostCtx, render_string

        ctxs: dict[str, HostCtx] = {}

        def render(value: Any, ctx: HostCtx) -> Any:
            if isinstance(value, str):
                return render_string(value, ctx) if "{{" in value or "{%" in value else value
            if isinstance(value, dict):
                return {k: render(v, ctx) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return type(value)(render(v, ctx) for v in value)
            return value

        for req in self.requirements(host):
            scope = (req.params or {}).get("scope", "admin")
            ctx = ctxs.setdefault(scope, HostCtx.for_host(host, scope))
            yield Requirement(render(req.name, ctx), req.kind, render(req.params or {}, ctx))

    def plan(self, host: HostConfig, facts: dict[str, Any]) -> Iterator[PlanEntry]:
        """Default planner: requirement satisfied? -> OK/MISSING/DRIFTED."""
        from nira.core.model import Op, PlanEntry

        for req in self.resolved(host):
            validate_requirement(req, host.os)
            yield PlanEntry(
                bundle=self.name,
                component=req.name,
                op=Op.INSTALL,
                status=self._assess(req, facts),
                detail="",
                kind=req.kind,
                params=dict(req.params),
            )

    def _assess(self, req: Requirement, facts: dict[str, Any]) -> Status:
        from nira.core.model import Status

        found = facts.get(req.kind, {}).get(req.name)
        if found is None or found is False:
            return Status.MISSING
        if found is True:
            return Status.OK
        if isinstance(found, dict) and (
            found.get("ok") is True or found.get("state") in ("active", "running", "ok")
        ):
            return Status.OK
        return Status.DRIFTED


REGISTRY: dict[str, type[Bundle]] = {}


def register(cls: type[Bundle]) -> type[Bundle]:
    REGISTRY[cls.name] = cls
    return cls


def get_bundle(name: str) -> Bundle:
    if name not in REGISTRY:
        # ensure builtins are imported
        import nira.bundles  # noqa: F401
    return REGISTRY[name]()
