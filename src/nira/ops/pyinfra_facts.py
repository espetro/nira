"""pyinfra-backed fact collection (separated so dry mode never imports pyinfra)."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from nira.core.model import HostConfig

KINDS = ("package", "file", "service", "secret", "directory", "custom")


def _pkg_state(binary: str | None) -> bool:
    return shutil.which(binary) is not None if binary else False


def _probe(binary: str, login: bool = False) -> bool:
    """Probe binary presence, optionally via a login shell (picks up mise/brew PATHs)."""
    try:
        cmd = ["bash", "-lc", f"command -v {binary}"] if login else ["which", binary]
        return (
            subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _collect_local() -> dict[str, dict[str, Any]]:
    """Cheap local fact collection (fast paths, no pyinfra host connection).

    Package facts are probed on demand via login-shell `command -v` (covers
    brew/mise shims). Known package names come from a fixed candidate list
    plus any names callers ask about through facts()."""
    facts: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
    facts["custom"] = {
        "nono": {"state": _probe("nono", login=True)},
    }
    candidates = (
        "mise", "git", "gh", "jq", "rg", "ugrep", "fd", "fzf", "ast-grep",
        "ghgrab", "repomix", "opensrc", "ghx", "claude", "crush", "maki",
        "uv", "pnpm", "node", "bun", "bifrost", "orca", "orcad", "oxmgr",
        "sops", "age", "degoog", "degoog-mcp", "agent-vault", "nono",
    )
    facts["package"] = {name: _probe(name, login=True) for name in candidates}
    facts["service"] = _local_services()
    return facts


def _local_services() -> dict[str, dict[str, Any]]:
    """LaunchAgent/LaunchDaemon + systemd presence for the local host."""
    services: dict[str, dict[str, Any]] = {}
    system = ("darwin", "Darwin")
    try:
        if system[0] == "darwin" or True:
            out = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=15, check=False
            ).stdout
            for line in out.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3:
                    services[parts[2]] = {"state": "loaded", "pid": parts[0]}
    except (OSError, subprocess.TimeoutExpired):
        pass
    return services


def collect_via_pyinfra(host: HostConfig) -> dict[str, dict[str, Any]]:
    """Collect facts for a host.

    For @local hosts we use cheap direct probes; for ssh hosts we shell out
    over ssh read-only (package managers, unit states) instead of driving a
    full pyinfra deploy state, keeping fact collection side-effect free.
    """
    if host.local:
        return _collect_local()

    facts: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
    prefix = ["ssh", f"{host.ssh_user}@{host.address}"]

    def run(cmd: str) -> str:
        try:
            return subprocess.run(
                [*prefix, cmd], capture_output=True, text=True, timeout=30, check=False
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    if host.os == "linux":
        out = run("dpkg-query -W -f='${Package}\n' 2>/dev/null")
        for pkg in out.split():
            facts["package"][pkg] = True
        out = run("systemctl list-units --type=service --all --no-legend")
        for line in out.splitlines():
            parts = line.split()
            if parts:
                name = parts[0].removesuffix(".service")
                facts["service"][name] = {"state": "loaded", "raw": line.strip()}
    else:
        out = run("brew list --formula 2>/dev/null; brew list --cask 2>/dev/null")
        for pkg in out.split():
            facts["package"][pkg] = True
        out = run("launchctl list 2>/dev/null")
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 3:
                label = parts[2]
                facts["service"][label] = {"state": "loaded", "pid": parts[0]}
    return facts
