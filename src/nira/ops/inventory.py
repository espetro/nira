"""Map HostConfig to pyinfra inventory parameters."""

from __future__ import annotations

from typing import Any

from nira.core.model import HostConfig


def host_inventory(host: HostConfig) -> tuple[str, dict[str, Any]]:
    """Return (pyinfra host name, connector data) for the target.

    pyinfra's paramiko connector needs explicit ssh_hostname/ssh_user data;
    a bare user@host name fails DNS resolution in paramiko."""
    if host.local:
        return "@local", {"os": host.os}
    return host.name, {
        "os": host.os,
        "ssh_hostname": host.address,
        "ssh_user": host.ssh_user,
        "ssh_look_for_keys": True,
        "ssh_allow_agent": True,
    }
