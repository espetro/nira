"""Shared bundle helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.core.bundle import Requirement


def home(host: HostConfig) -> str:
    """Home directory path on the target host (plan-time safe for ssh targets)."""
    if host.local:
        import os
        return os.path.expanduser('~')
    return f'/Users/{host.ssh_user}' if host.os == 'macos' else f'/home/{host.ssh_user}'


def pkg(name: str) -> Requirement:
    return Requirement(name=name, kind='package')
