"""Fleet manifest.yaml as a validated source, cross-checked against the bundle REGISTRY.

``load_fleet_manifest`` parses and schema-validates ``manifest.yaml`` (pydantic).
``cross_check`` compares it with the Python bundles and returns every
``Disagreement`` (it never raises on a mismatch). ``filter_known`` subtracts a
documented allow-list of known gaps (``tests/known_manifest_gaps.yaml``).

Coverage rules (manifest side -> bundle side):
  * ``packages`` / ``tools``: a package requirement of that name (or one that
    resolves to the same system package, or ``PACKAGE_ALIASES``), or a mise
    requirement for that tool.
  * ``mise_tools``: a mise requirement for that tool.
  * ``files``: a file requirement whose home-relative destination matches
    (``~/x`` == ``{{ home }}/x``); a same-named file at another destination is
    reported as ``file-dest-mismatch``.
  * ``repos``: a directory requirement with the same repo url (owner/name
    normalised) and destination.
  * ``services``: a service requirement of that name.
Reverse direction: package/mise/service requirements of a bundle that the
manifest never lists are ``extra-in-bundle``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# manifest name -> bundle package name, for deliberate renames only
PACKAGE_ALIASES: dict[str, str] = {}

# Bundles that may exist in Python without a manifest entry (documented allow-list).
UNMANAGED_BUNDLES: frozenset[str] = frozenset()


class ManifestError(ValueError):
    """manifest.yaml is missing or fails schema validation."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestFile(_Strict):
    path: str
    source: str | None = None
    mode: str | None = None
    secret: bool = False
    protected: bool = False


class ManifestRepo(_Strict):
    url: str
    path: str


class ManifestRequirements(_Strict):
    hosts: list[str] = Field(default_factory=list)
    packages: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mise_tools: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    files: list[ManifestFile] = Field(default_factory=list)
    repos: list[ManifestRepo] = Field(default_factory=list)


class ManifestBundle(_Strict):
    description: str
    enabled: bool = True
    requirements: ManifestRequirements = Field(default_factory=ManifestRequirements)


class ManifestFleet(_Strict):
    name: str
    tailscale_domain: str | None = None
    template_vars: dict[str, str] = Field(default_factory=dict)


class FleetManifest(_Strict):
    schema_version: int
    fleet: ManifestFleet
    bundles: dict[str, ManifestBundle]
    protected: list[str] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _v1(cls, v: int) -> int:
        if v != 1:
            raise ValueError("schema_version must be 1")
        return v

    @field_validator("bundles")
    @classmethod
    def _nonempty(cls, v: dict[str, ManifestBundle]) -> dict[str, ManifestBundle]:
        if not v:
            raise ValueError("bundles must be a non-empty mapping")
        return v


def load_fleet_manifest(path: Path) -> FleetManifest:
    """Parse + validate ``manifest.yaml`` (a file, or a fleet dir containing one)."""
    path = Path(path)
    if path.is_dir():
        path = path / "manifest.yaml"
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: cannot parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: top level must be a mapping")
    try:
        return FleetManifest.model_validate(data)
    except ValidationError as exc:
        lines = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        raise ManifestError(f"{path}: invalid manifest:\n  " + "\n  ".join(lines)) from exc


# ---------------------------------------------------------------- disagreements


@dataclass(frozen=True)
class Disagreement:
    bundle: str
    kind: str
    item: str
    detail: str = ""

    def __str__(self) -> str:
        tail = f": {self.detail}" if self.detail else ""
        return f"[{self.bundle}] {self.kind} {self.item!r}{tail}"


def norm_home_path(p: str) -> str | None:
    """Home-relative form of ``~/x`` or ``{{ home }}/x``; None if not home-anchored."""
    if p.startswith("~/"):
        return p[2:]
    m = re.match(r"^\{\{\s*home\s*\}\}/?(.*)$", p)
    return m.group(1) if m else None


def norm_repo(url: str) -> str:
    """``git@github.com:o/r.git`` and ``https://github.com/o/r`` -> ``github.com/o/r``."""
    u = url.strip()
    u = re.sub(r"^(?:https?://|ssh://)?(?:git@)?", "", u)
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/0-9]", u) else u
    u = re.sub(r"\.git$", "", u).rstrip("/")
    return u.lower()


def _pkg_ident(name: str) -> set[str]:
    """Names a package requirement answers to (logical name + resolved system names)."""
    from nira.core.packages import PackageError, resolve_package

    ids = {name}
    for os_name in ("linux", "macos"):
        try:
            spec = resolve_package(name, os_name)
        except PackageError:
            continue
        ids.add(spec.package)
    return ids


@dataclass
class _BundleView:
    packages: set[str]  # identities of package requirements
    package_names: set[str]
    mise_tools: set[str]
    services: set[str]
    files: dict[str, str]  # home-relative dest (or raw name) -> requirement name
    dirs: dict[str, str]  # home-relative dest -> repo
    sample_hosts: int


def _sample_hosts(name: str, hosts: Iterable[Any], oses: Iterable[str]) -> list[Any]:
    from nira.core.model import HostConfig

    chosen = [h for h in hosts if name in h.bundles]
    if chosen:
        return chosen
    return [
        HostConfig(name=f"sample-{o}", ssh_user="admin", address="x", os=o, bundles=[name])
        for o in oses
    ]


def _view(bundle: Any, hosts: list[Any]) -> _BundleView:
    v = _BundleView(set(), set(), set(), set(), {}, {}, len(hosts))
    for h in hosts:
        if not bundle.supports(h):
            continue
        for r in bundle.requirements(h):
            p = r.params or {}
            if r.kind == "package":
                v.package_names.add(r.name)
                v.packages |= _pkg_ident(r.name)
            elif r.kind == "custom" and "mise" in p:
                v.mise_tools.add(p.get("tool") or r.name.removeprefix("mise:"))
            elif r.kind == "service":
                v.services.add(r.name)
            elif r.kind == "file":
                v.files[norm_home_path(r.name) or r.name] = r.name
            elif r.kind == "directory":
                v.dirs[norm_home_path(r.name) or r.name] = p.get("repo", "")
    return v


def cross_check(
    manifest: FleetManifest,
    registry: dict[str, type] | None = None,
    hosts: Iterable[Any] = (),
) -> list[Disagreement]:
    """Every disagreement between ``manifest`` and the bundle registry (never raises)."""
    if registry is None:
        import nira.bundles  # noqa: F401
        from nira.core.bundle import REGISTRY

        registry = REGISTRY
    hosts = list(hosts)
    out: list[Disagreement] = []

    for name in sorted(manifest.bundles):
        if name not in registry:
            out.append(Disagreement(name, "bundle-missing-in-python", name, "no such Bundle"))
    for name in sorted(registry):
        cls = registry[name]
        if name in manifest.bundles or name in UNMANAGED_BUNDLES:
            continue
        if getattr(cls, "enabled", True) is False:
            continue  # disabled bundles may be absent from the manifest
        out.append(Disagreement(name, "bundle-missing-in-manifest", name, "not in manifest"))

    for name, mb in sorted(manifest.bundles.items()):
        cls = registry.get(name)
        if cls is None:
            continue
        bundle = cls()
        req = mb.requirements
        for hn in req.hosts:
            host = next((h for h in hosts if h.name == hn), None)
            if host is not None and not bundle.supports(host):
                out.append(
                    Disagreement(name, "host-unsupported", hn, f"bundle does not support os {host.os}")
                )
        if getattr(cls, "enabled", True) is False and mb.enabled:
            out.append(
                Disagreement(name, "enabled-mismatch", name, "Python bundle disabled, manifest enabled")
            )
        view = _view(bundle, _sample_hosts(name, hosts, ("linux", "macos")))
        listed_pkgs = set(req.packages) | set(req.tools)
        listed_ids: set[str] = set()

        for p in req.packages + req.tools:
            ids = _pkg_ident(PACKAGE_ALIASES.get(p, p))
            listed_ids |= ids
            kind = "package-uncovered" if p in req.packages else "tool-uncovered"
            if not (ids & view.packages) and p not in view.mise_tools:
                out.append(Disagreement(name, kind, p, "no package/mise requirement in bundle"))
        for t in req.mise_tools:
            if t not in view.mise_tools:
                out.append(Disagreement(name, "mise-tool-uncovered", t, "no mise requirement"))
        for s in req.services:
            if s not in view.services:
                out.append(Disagreement(name, "service-uncovered", s, "no service requirement"))

        for f in req.files:
            dest = norm_home_path(f.path) or f.path
            if dest in view.files:
                continue
            base = Path(f.path).name
            near = [d for d in view.files if Path(d).name == base]
            if near:
                out.append(
                    Disagreement(
                        name, "file-dest-mismatch", base,
                        f"manifest {f.path!r} vs bundle {view.files[near[0]]!r}",
                    )
                )
            else:
                out.append(Disagreement(name, "file-uncovered", f.path, "no file requirement"))
        for r in req.repos:
            dest = norm_home_path(r.path) or r.path
            want = norm_repo(r.url)
            have = {d: norm_repo(u) for d, u in view.dirs.items()}
            cand = (
                dest
                if dest in have
                else next((d for d, u in have.items() if u == want), None)
                or next((d for d, u in have.items() if u.rsplit("/", 1)[-1] == want.rsplit("/", 1)[-1]), None)
            )
            if cand is None:
                out.append(Disagreement(name, "repo-uncovered", r.url, "no directory requirement"))
                continue
            if have[cand] != want:
                out.append(
                    Disagreement(
                        name, "repo-url-mismatch", r.url, f"manifest {want} vs bundle {have[cand]}"
                    )
                )
            if cand != dest:
                out.append(
                    Disagreement(
                        name, "repo-dest-mismatch", r.url, f"manifest {r.path!r} vs bundle {cand!r}"
                    )
                )

        # reverse: bundle requirements the manifest never mentions
        mentioned = listed_pkgs | listed_ids | set(req.mise_tools)
        for pn in sorted(view.package_names):
            if not (_pkg_ident(pn) & mentioned) and pn not in mentioned:
                out.append(Disagreement(name, "extra-in-bundle", pn, "package not in manifest"))
        for t in sorted(view.mise_tools):
            if t not in mentioned:
                out.append(Disagreement(name, "extra-in-bundle", f"mise:{t}", "not in manifest"))
        for s in sorted(view.services):
            if s not in req.services:
                out.append(Disagreement(name, "extra-in-bundle", f"service:{s}", "not in manifest"))
    return out


# ---------------------------------------------------------------- allow-list


def load_known_gaps(path: Path) -> list[dict[str, str]]:
    """``tests/known_manifest_gaps.yaml``: ``gaps: [{bundle, kind, item, reason}]``.

    ``bundle``/``kind``/``item`` accept fnmatch globs (``*`` for any)."""
    path = Path(path)
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    gaps = data.get("gaps") or []
    for g in gaps:
        missing = {"bundle", "kind", "item", "reason"} - set(g)
        if missing:
            raise ManifestError(f"{path}: gap {g} missing {sorted(missing)}")
    return gaps


def _matches(d: Disagreement, g: dict[str, str]) -> bool:
    return (
        fnmatchcase(d.bundle, g["bundle"])
        and fnmatchcase(d.kind, g["kind"])
        and fnmatchcase(d.item, g["item"])
    )


def filter_known(
    found: list[Disagreement], known: list[dict[str, str]]
) -> tuple[list[Disagreement], list[dict[str, str]]]:
    """(unexpected disagreements, stale allow-list entries that matched nothing)."""
    unexpected = [d for d in found if not any(_matches(d, g) for g in known)]
    stale = [g for g in known if not any(_matches(d, g) for d in found)]
    return unexpected, stale


# ---------------------------------------------------------------- template vars


def template_vars_for(manifest: FleetManifest | None, overrides: dict[str, Any], host: str) -> dict:
    """Extra Jinja variables for ``host``: manifest fleet vars < tailnet_domain < host overrides."""
    out: dict[str, str] = {}
    if manifest is not None:
        out.update(manifest.fleet.template_vars)
        if manifest.fleet.tailscale_domain:
            out.setdefault("tailnet_domain", manifest.fleet.tailscale_domain)
    tv = overrides.get(host, {}).get("template_vars")
    if isinstance(tv, dict):
        out.update({k: str(v) for k, v in tv.items()})
    return out

