"""bifrost-stack: bifrost binary + BIFROST_ENCRYPTION_KEY secret + config.json."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig
from nira.bundle_paths import hp
from nira.core.bundle import Bundle, Requirement, register
from nira.core.protect import is_protected

MAC_PLIST = "/Library/LaunchDaemons/io.bifrost.daemon.plist"
LINUX_UNIT = "/etc/systemd/system/bifrost.service"


@register
class BifrostStackBundle(Bundle):
    name = "bifrost-stack"
    description = "bifrost gateway: binary, encryption key secret, config.json"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        # mise github backend (see core.packages.MISE_SPECS["bifrost"]); on hosts that carry a
        # protected, pre-existing install (systemd unit) it is probed, never installed.
        yield Requirement(
            name="bifrost",
            kind="package",
            params={
                "scope": "agent",
                "verify_only": is_protected("bifrost", host.protected_components),
            },
        )
        yield Requirement(
            name="BIFROST_ENCRYPTION_KEY",
            kind="secret",
            params={
                "sops_file": "secrets/bifrost.yaml",
                "key": "BIFROST_ENCRYPTION_KEY",
                "method": "sops-exec-env",
            },
        )
        yield Requirement(
            name=hp(".config/bifrost/config.json"),
            kind="file",
            params={
                "scope": "agent",
                "template": "bifrost-config.json.j2",
                # config references the secret via env var, never inline value
                "env_ref": "BIFROST_ENCRYPTION_KEY",
            },
        )
        if host.os == "macos":
            yield Requirement(
                name="io.bifrost.daemon",  # launchd label; must match the facts key
                kind="service",
                params={"plist_path": MAC_PLIST, "bootstrap": True},
            )
        else:
            yield Requirement(
                name="bifrost",  # systemd unit name; must match the facts key
                kind="service",
                params={"unit_path": LINUX_UNIT},
            )
