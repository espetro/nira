"""Per-OS package resolution.

Requirement names are logical ("fd", "mise"); each OS installs them under a
different package name, through a different manager, or not through the
system package manager at all. Resolution happens at plan time so a bad name
fails loudly before anything touches a host.

Resolution order for a ``package`` requirement:
    1. the OS package manager (apt / brew name maps),
    2. the mise spec map (``MISE_SPECS``),
    3. otherwise the name is passed to the OS manager as written.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field


class PackageError(ValueError):
    """A package requirement cannot be satisfied on this OS as written."""


@dataclass(frozen=True)
class MiseSpec:
    """A tool installed through mise (``mise use -g <spec>``)."""

    spec: str | None  # backend spec (aqua:/github:/npm:/cargo: or registry shortname)
    bin: str | None = None  # executable it provides (default: last spec component)
    version: str = "latest"
    version_prefix: str | None = None  # release-tag prefix mise strips (e.g. "transports/v")
    # per-platform download URLs: mise platform key -> url (may use {{version}})
    platforms: Mapping[str, str] = field(default_factory=dict)
    verify_only: bool = False  # no installable artifact: probe only
    note: str = ""

    @property
    def needs_config(self) -> bool:
        """True if a plain ``mise use -g`` cannot express this tool."""
        return bool(self.platforms or self.version_prefix)


@dataclass(frozen=True)
class PackageSpec:
    manager: str  # "apt" | "brew" | "cask" | "mise-installer" | "mise" | "external"
    package: str  # name as the manager knows it (the mise spec for "mise")
    binary: str | None = None  # executable to check on PATH after install
    mise: MiseSpec | None = None  # set for manager "mise" / "external"
    verify_only: bool = False  # probe, never install
    note: str = ""


# logical name -> (apt package, binary on PATH)
APT: dict[str, tuple[str, str | None]] = {
    "git": ("git", "git"),
    "jq": ("jq", "jq"),
    "rsync": ("rsync", "rsync"),
    "gh": ("gh", "gh"),
    "ripgrep": ("ripgrep", "rg"),
    "rg": ("ripgrep", "rg"),
    "ugrep": ("ugrep", "ugrep"),
    "fd": ("fd-find", "fdfind"),  # Debian ships the binary as fdfind
    "fd-find": ("fd-find", "fdfind"),
    "fzf": ("fzf", "fzf"),
    "age": ("age", "age"),
    "ffmpeg": ("ffmpeg", "ffmpeg"),
    "sox": ("sox", "sox"),
    "tailscale": ("tailscale", "tailscale"),
}

# names brew provides that apt's map above does not list
BREW_NAMES = frozenset(APT) | {"sops", "yt-dlp"}

BREW_BINARY = {"ripgrep": "rg", "fd": "fd", "ugrep": "ugrep", "sops": "sops"}

_BIFROST_URL = "https://downloads.getmaxim.ai/bifrost/v{{version}}/%s/bifrost-http"

# Names the OS package manager does not provide -> mise spec.
MISE_SPECS: dict[str, MiseSpec] = {
    "ast-grep": MiseSpec("aqua:ast-grep/ast-grep", bin="ast-grep"),
    "repomix": MiseSpec("npm:repomix", bin="repomix"),
    "opensrc": MiseSpec("npm:opensrc", bin="opensrc"),
    "ghgrab": MiseSpec("github:abhixdd/ghgrab", bin="ghgrab"),
    "ghx": MiseSpec("github:brunoborges/ghx", bin="ghx"),
    "sops": MiseSpec("aqua:getsops/sops", bin="sops"),
    "yt-dlp": MiseSpec("github:yt-dlp/yt-dlp", bin="yt-dlp"),
    "claude": MiseSpec("aqua:anthropics/claude-code", bin="claude"),
    "bifrost": MiseSpec(
        "github:maximhq/bifrost",
        bin="bifrost",
        version_prefix="transports/v",
        platforms={
            "macos-arm64": _BIFROST_URL % "darwin/arm64",
            "macos-x64": _BIFROST_URL % "darwin/amd64",
            "linux-x64": _BIFROST_URL % "linux/amd64",
            "linux-arm64": _BIFROST_URL % "linux/arm64",
        },
    ),
    # degoog-org/mcp ships no release assets and its npm package is private (docker/bun only)
    "degoog-mcp": MiseSpec(
        None,
        bin="degoog-mcp",
        verify_only=True,
        note="no installable artifact (docker/bun source only); install it by hand",
    ),
    # orca is an Electron app; its bundle is disabled, kept report-only
    "orca": MiseSpec(None, bin="orca", verify_only=True, note="vendor app; bundle disabled"),
    "orcad": MiseSpec(None, bin="orcad", verify_only=True, note="vendor app; bundle disabled"),
}

# registry shortnames that collide: the bare name is a different tool than intended
AMBIGUOUS_MISE_SHORTNAMES = {
    "fx": "github:vercel-labs/fx (the registry shortname 'fx' resolves to antonmedv/fx)",
}
_SPEC_RE = re.compile(r"^[A-Za-z0-9_.:@/+-]+$")
_VERSIONED_RE = re.compile(r"^(.*?[^:/])@[^@/]*$")  # strips a trailing @version, not npm scopes
_OWNER_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_BACKENDS = (
    "aqua", "github", "gitlab", "npm", "cargo", "pipx", "go", "ubi", "http", "asdf", "vfox",
)  # fmt: skip


@functools.lru_cache(maxsize=1)
def mise_registry() -> dict[str, list[str]] | None:
    """Offline ``mise registry`` as {shortname: [backends]}; None if mise is unavailable."""
    exe = shutil.which("mise")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "registry"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    reg: dict[str, list[str]] = {}
    for line in out.splitlines():
        parts = line.split()
        if parts:
            reg[parts[0]] = parts[1:]
    return reg or None


def check_mise_spec(spec: str, *, online: bool | None = None) -> None:
    """Validate a mise tool spec; raises PackageError.

    Backend-prefixed specs are checked syntactically; bare shortnames must exist in
    the (offline) ``mise registry`` when mise is installed. With NIRA_ONLINE=1 (or
    ``online=True``) the spec must also list versions via ``mise ls-remote``.
    """
    m = _VERSIONED_RE.match(spec)
    base = m.group(1) if m else spec
    if base in AMBIGUOUS_MISE_SHORTNAMES:
        raise PackageError(
            f"mise tool {spec!r} is ambiguous: use {AMBIGUOUS_MISE_SHORTNAMES[base]}"
        )
    if not _SPEC_RE.match(spec):
        raise PackageError(f"mise tool spec {spec!r} has unsupported characters")
    if ":" in base:
        backend, _, rest = base.partition(":")
        if backend not in _BACKENDS or not rest:
            raise PackageError(f"mise tool spec {spec!r}: unknown backend {backend!r}")
        if backend in ("github", "gitlab", "aqua") and not _OWNER_REPO_RE.match(rest):
            raise PackageError(f"mise tool spec {spec!r}: expected {backend}:owner/repo")
    else:
        reg = mise_registry()
        if reg is not None and base not in reg:
            raise PackageError(f"mise tool {spec!r} is not in the mise registry")
    if online is None:
        online = os.environ.get("NIRA_ONLINE") == "1"
    exe = shutil.which("mise") if online else None
    if exe:
        try:
            res = subprocess.run(
                [exe, "ls-remote", base], capture_output=True, text=True, timeout=60, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise PackageError(f"mise ls-remote {base} failed: {exc}") from exc
        if res.returncode != 0 or not res.stdout.strip():
            raise PackageError(f"mise tool {spec!r} resolves to no versions (ls-remote failed)")


def mise_bin(spec: str | None, bin_: str | None, fallback: str) -> str:
    """Executable a mise spec provides: explicit ``bin`` or the spec's last component."""
    if bin_:
        return bin_
    if spec:
        return re.split(r"[/:]", spec)[-1].split("@")[0]
    return fallback


def _mise_package(name: str, ms: MiseSpec) -> PackageSpec:
    if ms.spec:
        check_mise_spec(ms.spec)
    return PackageSpec(
        "mise" if ms.spec else "external",
        ms.spec or name,
        mise_bin(ms.spec, ms.bin, name),
        mise=ms,
        verify_only=ms.verify_only,
        note=ms.note,
    )


def resolve_package(name: str, os_name: str) -> PackageSpec:
    """Map a logical package name to an installable spec for ``os_name``.

    Order: OS package manager (apt/brew map) -> mise spec map -> the name as
    written for the OS manager. Impossible requests (casks on linux, unknown OS)
    raise PackageError.
    """
    if os_name not in ("macos", "linux"):
        raise PackageError(f"package {name!r}: unsupported os {os_name!r}")
    if name.startswith("cask:"):
        if os_name != "macos":
            raise PackageError(f"package {name!r}: casks are macOS-only")
        return PackageSpec("cask", name.removeprefix("cask:"))
    if name == "mise":
        if os_name == "macos":
            return PackageSpec("brew", "mise", "mise")
        return PackageSpec("mise-installer", "mise", "mise")
    if os_name == "macos":
        if name in BREW_NAMES:
            return PackageSpec("brew", name, BREW_BINARY.get(name, name))
    elif name in APT:
        pkg, binary = APT[name]
        return PackageSpec("apt", pkg, binary)
    if name in MISE_SPECS:
        return _mise_package(name, MISE_SPECS[name])
    if os_name == "macos":
        return PackageSpec("brew", name, BREW_BINARY.get(name, name))
    return PackageSpec("apt", name, name)
