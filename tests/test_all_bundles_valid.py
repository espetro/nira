"""Every registered bundle must validate for the fleet's host shapes (hermetic, no ssh)."""

from __future__ import annotations

import pytest

import nira.bundles  # noqa: F401 - populate registry
from nira.core.bundle import REGISTRY, validate_requirement
from nira.core.model import HostConfig

HOSTS = {
    "linux": HostConfig("ovhcloud-like", "debian", "x", "linux", agent_user="own-agents"),
    "macos": HostConfig("mac-like", "admin", "x", "macos", agent_user="own-agents"),
}


@pytest.mark.parametrize("os_name", sorted(HOSTS))
def test_all_bundles_validate(os_name: str) -> None:
    host = HOSTS[os_name]
    errors: list[str] = []
    checked = 0
    for name, cls in sorted(REGISTRY.items()):
        bundle = cls()
        if not bundle.supports(host):
            continue
        for req in bundle.resolved(host):
            checked += 1
            try:
                validate_requirement(req, os_name)
            except Exception as exc:  # noqa: BLE001 - collect everything
                errors.append(f"{name}: {exc}")
    assert checked > 20
    assert not errors, "\n".join(errors)
