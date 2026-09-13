"""pyinfra-backed fact collection (separated so dry mode never imports pyinfra)."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from nira.core.model import HostConfig

KINDS = ("package", "file", "service", "secret", "directory", "custom")


def _pkg_state(binary: str | None) -> bool:
    return shutil.which(binary) is not None if binary else False


def _collect_local() -> dict[str, dict[str, Any]]:
    """Cheap local fact collection (fast paths, no pyinfra host connection)."""
    facts: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
    facts["custom"] = {
        "nono": {"state": shutil.which("nono") is not None},
    }
    return facts


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
