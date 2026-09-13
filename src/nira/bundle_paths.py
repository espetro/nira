"""Per-host path resolution helpers (plan-time safe for remote hosts)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig


def home(host: HostConfig) -> str:
    """Home directory path on the target host."""
    if host.local:
        return os.path.expanduser('~')
    if host.os == 'macos':
        return f'/Users/{host.ssh_user}'
    return f'/home/{host.ssh_user}'
