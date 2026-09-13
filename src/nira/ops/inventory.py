"""Map HostConfig to pyinfra inventory parameters."""

from __future__ import annotations

from typing import Any

from nira.core.model import HostConfig


def host_inventory(host: HostConfig) -> tuple[str, dict[str, Any]]:
    """Return (pyinfra host string, resolved data) for the target."""
    if host.local:
        return "@local", {"os": host.os}
    return f"{host.ssh_user}@{host.address}", {
        "os": host.os,
        "ssh_user": host.ssh_user,
    }
