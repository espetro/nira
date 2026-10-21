"""Fact collection: one read-only probe per requirement.

Facts are keyed ``{kind: {requirement name: state}}`` where state is True
(satisfied), False (missing) or ``{"drift": detail}`` (present but not
satisfying). Probes run in a single batched shell script, locally or over ssh.
An unreachable host raises HostUnreachable: "ssh failed" must never be
reported as "nothing installed".
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from typing import Any

from nira.core.model import HostConfig
from nira.ops import probes

KINDS = ("package", "file", "service", "secret", "directory", "custom")

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


class HostUnreachable(RuntimeError):
    """The target could not be reached or the probe transport failed."""


class ProbeError(RuntimeError):
    """A probe itself is broken (command missing, sudo refused), not 'absent'."""


def run_script(host: HostConfig, script: str, timeout: int = 120) -> str:
    """Run a shell script on the target; raise HostUnreachable on transport failure."""
    if host.local:
        cmd = ["bash", "-s"]
    else:
        cmd = ["ssh", *SSH_OPTS, f"{host.ssh_user}@{host.address}", "bash", "-s"]
    try:
        p = subprocess.run(
            cmd, input=script, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostUnreachable(f"{host.name}: {exc}") from exc
    if not host.local and p.returncode == 255:
        raise HostUnreachable(f"{host.name}: ssh failed: {p.stderr.strip()[:200]}")
    if p.returncode != 0 and "__NIRA__" not in p.stdout:
        raise HostUnreachable(
            f"{host.name}: probe script exited {p.returncode}: {p.stderr.strip()[:200]}"
        )
    return p.stdout


def collect_via_pyinfra(
    host: HostConfig, requirements: Iterable[Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Probe every requirement and return facts for all kinds.

    (Name kept for compatibility: probing is plain shell, no pyinfra state is
    created, so collection stays side-effect free.)
    """
    facts: dict[str, dict[str, Any]] = {k: {} for k in KINDS}
    reqs = list(requirements or [])
    if not reqs:
        return facts

    ctx = probes.ctx_for_host(host)
    items: list[tuple[int, str]] = []
    for i, req in enumerate(reqs):
        snippet = probes.probe_snippet(req.kind, req.name, req.params, ctx)
        scope = probes.effective_scope(req.kind, req.name, req.params, host.os)
        items.append((i, probes.wrap(snippet, ctx, scope)))

    codes = probes.parse_results(run_script(host, probes.build_script(items)))
    for i, req in enumerate(reqs):
        if i not in codes:
            raise HostUnreachable(f"{host.name}: no result for probe {req.kind} {req.name!r}")
        rc = codes[i]
        bucket = facts.setdefault(req.kind, {})
        if rc == probes.OK:
            bucket[req.name] = True
        elif rc == probes.MISSING:
            bucket[req.name] = False
        elif rc == probes.DRIFT:
            bucket[req.name] = {"drift": "present but not satisfying (inactive/mismatch)"}
        elif rc == probes.SUDO_FAILED:
            raise ProbeError(
                f"{host.name}: cannot run as agent user {ctx.agent_user!r} "
                f"(sudo -n refused) while probing {req.kind} {req.name!r}"
            )
        else:
            raise ProbeError(f"{host.name}: probe {req.kind} {req.name!r} errored (rc={rc})")
    return facts
