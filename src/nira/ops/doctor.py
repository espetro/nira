"""Doctor checks: read-only host health diagnostics."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _run_local(cmd: list[str], timeout: int = 15) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode == 0, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def run_checks(host: HostConfig) -> list[CheckResult]:
    checks: list[CheckResult] = []
    prefix = ["ssh", f"{host.ssh_user}@{host.address}", "-o", "ConnectTimeout=10"]

    # ssh reachability
    if host.local:
        checks.append(CheckResult("ssh", True, "@local host, no ssh needed"))
    else:
        ok, out = _run_local([*prefix, "true"], timeout=20)
        checks.append(CheckResult("ssh", ok, out or "reachable"))

    # tailscale (local only)
    if shutil.which("tailscale"):
        ok, out = _run_local(["tailscale", "status"])
        checks.append(CheckResult("tailscale", ok, out.splitlines()[0] if out else "ok"))
    else:
        checks.append(CheckResult("tailscale", False, "tailscale binary not found"))

    # init system unit states
    if host.os == "macos":
        ok, out = _run_local([*prefix] if not host.local else ["launchctl", "list"])
        if host.local:
            checks.append(CheckResult("launchd", ok, "launchctl list ok"))
        else:
            ok, out = _run_local([*prefix, "launchctl list"], timeout=20)
            checks.append(
                CheckResult("launchd", ok, (out.splitlines() or [""])[0][:80] if out else "ok")
            )
    else:
        cmd = ["true"] if host.local else "systemctl is-system-running"
        if host.local:
            checks.append(CheckResult("systemd", True, "local linux host"))
        else:
            ok, out = _run_local([*prefix, cmd], timeout=20)
            checks.append(CheckResult("systemd", ok, out or "ok"))

    # orca pairing state: config presence only (never mutate)
    orca_cfg = "/Users/josocjoq/.config/orca"
    if host.os == "macos":
        checks.append(
            CheckResult("orca-pairing", _run_local(["test", "-d", orca_cfg])[0], "config dir")
        )
    else:
        if not host.local:
            ok, out = _run_local([*prefix, "test -d ~/.config/orca && echo yes"], timeout=15)
            checks.append(CheckResult("orca-pairing", "yes" in out, "config dir"))
        else:
            checks.append(CheckResult("orca-pairing", False, "n/a"))

    # sops decryptability
    if shutil.which("sops"):
        checks.append(CheckResult("sops", True, "sops available"))
    else:
        checks.append(CheckResult("sops", False, "sops binary not found"))

    return checks
