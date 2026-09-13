"""bifrost-stack: bifrost binary + BIFROST_ENCRYPTION_KEY secret + config.json."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig
from pathlib import Path

from nira.core.bundle import Bundle, Requirement, register

BIFROST_DIR = Path.home() / ".config" / "bifrost"
MAC_PLIST = "/Library/LaunchDaemons/io.bifrost.daemon.plist"
LINUX_UNIT = "/etc/systemd/system/bifrost.service"


@register
class BifrostStackBundle(Bundle):
    name = "bifrost-stack"
    description = "bifrost gateway: binary, encryption key secret, config.json"

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        yield Requirement(name="bifrost", kind="package")
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
            name=str(BIFROST_DIR / "config.json"),
            kind="file",
            params={
                "template": "bifrost-config.json.j2",
                # config references the secret via env var, never inline value
                "env_ref": "BIFROST_ENCRYPTION_KEY",
            },
        )
        if host.os == "macos":
            yield Requirement(
                name="bifrost-daemon",
                kind="service",
                params={"plist_path": MAC_PLIST, "bootstrap": True},
            )
        else:
            yield Requirement(
                name="bifrost-daemon",
                kind="service",
                params={"unit_path": LINUX_UNIT},
            )
