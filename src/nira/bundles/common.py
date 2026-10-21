"""Shared bundle helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.bundle_paths import home as _home
from nira.core.bundle import Requirement


def home(host: HostConfig) -> str:
    """Symbolic home of the run-as user (resolved at apply time)."""
    return _home(host)


def pkg(name: str) -> Requirement:
    return Requirement(name=name, kind='package')
