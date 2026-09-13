"""secret-broker: agent-vault daemon seeded from sops. Applied first (priority 0)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

from nira.core.bundle import Bundle, Requirement, register

VAULT_PLIST = "/Library/LaunchDaemons/io.nira.agent-vault.plist"
VAULT_UNIT = "/etc/systemd/system/agent-vault.service"


@register
class SecretBrokerBundle(Bundle):
    name = "secret-broker"
    description = "agent-vault daemon seeded from sops; applied before all other bundles"
    priority = 0  # others default 50; lower runs first

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(
            name="agent-vault-seed",
            kind="secret",
            params={
                "sops_file": "secrets/agent-vault.yaml",
                "method": "sops-exec-env",  # never plaintext on disk
            },
        )
        if host.os == "macos":
            yield Requirement(
                name="agent-vault",
                kind="service",
                params={"plist_path": VAULT_PLIST, "bootstrap": True},
            )
        else:
            yield Requirement(
                name="agent-vault",
                kind="service",
                params={"unit_path": VAULT_UNIT},
            )
