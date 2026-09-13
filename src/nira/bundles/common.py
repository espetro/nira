"""Shared helpers for bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nira.core.bundle import Requirement

if TYPE_CHECKING:
    from nira.core.model import HostConfig


def pkg(name: str) -> Requirement:
    return Requirement(name=name, kind="package")


def host_os(host: HostConfig) -> str:
    return host.os


# Protected component names: report-only, never mutated.
PROTECTED = ("web-services", "brioso-", "xvfb-orca", "orca.service", "orcad")
