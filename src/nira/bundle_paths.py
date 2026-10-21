"""Per-host path helpers.

Paths are symbolic (``{{ home }}/...``) at plan time; the home of the run-as
user is resolved at apply time (see nira.core.paths / nira.core.render_tpl).
"""

from __future__ import annotations

from nira.core.paths import HostPath

__all__ = ["HostPath", "home", "hp"]


def home(host: object = None) -> str:
    """Symbolic home token (host-independent on purpose)."""
    return str(HostPath.home(""))


def hp(rel: str) -> str:
    """Symbolic string for a path under the run-as user's home."""
    return str(HostPath.home(rel))
