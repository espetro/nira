"""Doctor checks: read-only host health diagnostics, run against the TARGET host."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nira.ops import probes

if TYPE_CHECKING:
    from nira.core.model import HostConfig


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _run(host: HostConfig, script: str, timeout: int = 20, scope: str = "self") -> tuple[bool, str]:
    """Run a shell snippet on the target (locally for @local hosts, else over ssh)."""
    ctx = probes.ctx_for_host(host)
    cmd = probes.wrap(script, ctx, scope)
    if host.local:
        argv = ["bash", "-c", cmd]
    else:
        argv = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            f"{host.ssh_user}@{host.address}", cmd,
        ]  # fmt: skip
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def run_checks(host: HostConfig) -> list[CheckResult]:
    checks: list[CheckResult] = []

    # reachability of the target
    if host.local:
        checks.append(CheckResult("ssh", True, "@local host, no ssh needed"))
    else:
        ok, out = _run(host, "true")
        checks.append(CheckResult("ssh", ok, out or "reachable"))

    # tailscale on the target
    ok, out = _run(host, "command -v tailscale >/dev/null 2>&1 && tailscale status | head -1")
    checks.append(
        CheckResult("tailscale", ok, out.splitlines()[0] if ok and out else "tailscale not usable")
    )

    # init system on the target
    if host.os == "macos":
        ok, out = _run(host, "launchctl list | head -1")
        checks.append(CheckResult("launchd", ok, out[:80] if out else "ok"))
    else:
        ok, out = _run(host, "systemctl is-system-running")
        # "degraded" still means systemd is up, but a failed unit deserves attention
        checks.append(CheckResult("systemd", ok and out == "running", out or "ok"))

    # orca pairing state: config presence in the agent's home (never mutate)
    ok, _ = _run(host, 'test -d "$HOME/.config/orca"', scope="agent")
    checks.append(CheckResult("orca-pairing", ok, "config dir"))

    # sops on the target
    ok, _ = _run(host, "command -v sops >/dev/null 2>&1")
    checks.append(CheckResult("sops", ok, "sops available" if ok else "sops binary not found"))

    return checks
