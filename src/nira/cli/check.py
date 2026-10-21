"""Offline fleet check: ``nira plan --all-hosts --check``.

Renders every requirement and template of every inventory host without ever
touching a host. Home directories come from a deterministic fake lookup
(``/Users/<u>`` on macos, ``/home/<u>`` on linux) unless ``home_of`` is injected.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateError

from nira.core.paths import HomeOf
from nira.core.render_tpl import PACKAGE_TEMPLATES, HostCtx


@dataclass(frozen=True)
class Issue:
    severity: str  # "error" | "warning" | "info"
    host: str
    bundle: str
    message: str

    def __str__(self) -> str:
        where = f"{self.host}/{self.bundle}" if self.bundle else self.host
        return f"{self.severity.upper():7} {where}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "host": self.host,
            "bundle": self.bundle,
            "message": self.message,
        }


@dataclass
class CheckReport:
    issues: list[Issue] = field(default_factory=list)
    hosts: int = 0

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def add(self, severity: str, host: str, bundle: str, message: str) -> None:
        self.issues.append(Issue(severity, host, bundle, message))


@dataclass
class CheckCtx(HostCtx):
    """HostCtx that also exposes fleet ``template_vars`` / ``tailnet_domain`` / ``host``."""

    extra: dict[str, str] = field(default_factory=dict)

    def vars(self) -> dict[str, str]:
        return {**self.extra, **super().vars()}


def fake_home_of(os_name: str) -> HomeOf:
    base = "/Users" if os_name == "macos" else "/home"
    return lambda user: f"{base}/{user}"


@contextlib.contextmanager
def _offline_homes(home_of: HomeOf | None) -> Iterator[None]:
    """Replace the ssh-backed home lookup used by ``Bundle.resolved`` for the duration."""
    import nira.core.render_tpl as rt

    saved = rt.default_home_of
    rt.default_home_of = lambda host: home_of or fake_home_of(host.os)  # type: ignore[assignment]
    try:
        yield
    finally:
        rt.default_home_of = saved


def _render_fleet_template(name: str, ctx: CheckCtx) -> str:
    """Render ``name`` strictly; ``X.j2`` falls back to the fleet's ``X.tmpl``."""
    loader = FileSystemLoader([str(p) for p in ctx.template_dirs])
    env = Environment(
        loader=loader, undefined=StrictUndefined, autoescape=False, keep_trailing_newline=True
    )
    candidates = [name, re.sub(r"\.j2$", ".tmpl", name)]
    last: Exception | None = None
    for cand in dict.fromkeys(candidates):
        try:
            return env.get_template(cand).render(**ctx.vars())
        except TemplateError as exc:
            if type(exc).__name__ != "TemplateNotFound":
                raise
            last = exc
    raise last  # type: ignore[misc]


def check_fleet(
    fleet: Path,
    *,
    home_of: HomeOf | None = None,
    probe: bool = False,
    hosts_filter: list[str] | None = None,
    plan_fn: Callable[[Any], Any] | None = None,
) -> CheckReport:
    """Statically validate the whole fleet. ``probe=True`` additionally plans over ssh."""
    import nira.bundles  # noqa: F401 - populate REGISTRY
    from nira.core.bundle import REGISTRY, RequirementError, validate_requirement
    from nira.core.manifest import ManifestError, load_fleet_manifest, template_vars_for
    from nira.core.model import load_hosts, load_overrides
    from nira.core.protect import BUILTIN_PROTECTED

    fleet = Path(fleet)
    rep = CheckReport()
    inv = fleet / "inventory"
    if not inv.is_dir():
        rep.add("error", "-", "", f"inventory dir not found: {inv}")
        return rep

    manifest = None
    if (fleet / "manifest.yaml").is_file():
        try:
            manifest = load_fleet_manifest(fleet)
        except ManifestError as exc:
            rep.add("error", "-", "", str(exc))
    overrides = load_overrides(fleet)
    hosts = load_hosts(inv, overrides)
    names = {h.name for h in hosts}
    for key in sorted(set(overrides) - names):
        rep.add("error", key, "", "overrides.yaml entry for a host not in the inventory")
    for hname, ov in overrides.items():
        tv = ov.get("template_vars")
        if tv is not None and not (
            isinstance(tv, dict) and all(isinstance(v, str | int | float) for v in tv.values())
        ):
            rep.add("error", hname, "", "overrides template_vars must be a mapping of scalars")

    tpl_dirs = [fleet / "agent-config", fleet / "templates", PACKAGE_TEMPLATES]
    protected_extra = set(manifest.protected) if manifest else set()

    with _offline_homes(home_of):
        for host in hosts:
            if hosts_filter and host.name not in hosts_filter:
                continue
            rep.hosts += 1
            _check_host(
                rep, host, fleet, manifest, overrides, tpl_dirs, protected_extra,
                REGISTRY, RequirementError, validate_requirement, BUILTIN_PROTECTED,
                template_vars_for, home_of,
            )
            if probe:
                _probe_host(rep, host, plan_fn)
    return rep


def _check_host(
    rep, host, fleet, manifest, overrides, tpl_dirs, protected_extra,
    registry, requirement_error, validate_requirement, builtin_protected,
    template_vars_for, home_of,
) -> None:
    extra = {"host": host.name, **template_vars_for(manifest, overrides, host.name)}
    names: set[str] = set()
    for bname in host.bundles:
        cls = registry.get(bname)
        if cls is None:
            rep.add("error", host.name, bname, "unknown bundle (not in REGISTRY)")
            continue
        if manifest is not None and bname not in manifest.bundles:
            rep.add("error", host.name, bname, "bundle not in manifest.yaml")
        if getattr(cls, "enabled", True) is False:
            rep.add("info", host.name, bname, "bundle disabled; skipped")
            continue
        bundle = cls()
        if not bundle.supports(host):
            rep.add("info", host.name, bname, f"unsupported on os {host.os}; skipped")
            continue
        try:
            reqs = list(bundle.resolved(host))
        except Exception as exc:  # noqa: BLE001 - jinja UndefinedError, HostPathError, ...
            rep.add("error", host.name, bname, f"cannot resolve requirements: {exc}")
            continue
        for req in reqs:
            names.add(req.name)
            try:
                validate_requirement(req, host.os)
            except requirement_error as exc:
                rep.add("error", host.name, bname, f"invalid requirement: {exc}")
                continue
            _check_content(rep, host, bname, req, fleet, tpl_dirs, extra, home_of)

    for pat in host.protected_components:
        if any(fnmatchcase(n, pat) for n in names):
            continue
        if pat in protected_extra or pat in builtin_protected:
            rep.add(
                "warning", host.name, "",
                f"protected pattern {pat!r} matches no managed component "
                "(guards an externally managed resource)",
            )
        else:
            rep.add("error", host.name, "", f"protected pattern {pat!r} matches no component")


def _check_content(rep, host, bname, req, fleet, tpl_dirs, extra, home_of) -> None:
    from nira.core.model import run_as

    p = req.params
    if req.kind == "file":
        if "template" in p:
            scope = p.get("scope", "admin")
            base = HostCtx.for_host(host, scope, home_of=home_of, template_dirs=tpl_dirs)
            ctx = CheckCtx(base.user, base.home_of, list(tpl_dirs), extra=dict(extra))
            try:
                _render_fleet_template(p["template"], ctx)
            except Exception as exc:  # noqa: BLE001
                kind = type(exc).__name__
                rep.add(
                    "error", host.name, bname,
                    f"template {p['template']!r} (as {run_as(host, scope)}): {kind}: {exc}",
                )
        elif p.get("verbatim"):
            src = fleet / "agent-config" / Path(req.name).name
            if not src.is_file():
                rep.add("error", host.name, bname, f"fleet file missing: {src.relative_to(fleet)}")
        elif "src" in p and not (fleet / p["src"]).is_file():
            rep.add("error", host.name, bname, f"fleet file missing: {p['src']}")
    elif req.kind == "secret" and "sops_file" in p and not (fleet / p["sops_file"]).is_file():
        rep.add("warning", host.name, bname, f"sops file not in fleet yet: {p['sops_file']}")


def _probe_host(rep: CheckReport, host: Any, plan_fn: Callable[[Any], Any] | None) -> None:
    from nira.core.planner import build_plan

    try:
        (plan_fn or build_plan)(host)
    except Exception as exc:  # noqa: BLE001
        rep.add("error", host.name, "", f"probe/plan failed: {exc}")
