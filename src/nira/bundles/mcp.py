"""mcp: bifrost MCP clients from the fleet's ``mcp/*.yaml``, one ``mcp`` requirement each.

Clients are created ``disabled`` unless the host's overrides list them in
``mcp_enable``. Stdio commands (``{home: rel}``) resolve for the agent user.
Host overrides read: ``mcp_enable``, ``mcp_virtual_key`` (grant enabled clients
to this VK), ``bifrost_admin_port`` (default 8317), ``mcp_creds_sops`` /
``mcp_creds_file`` (``{home: rel}`` or absolute path on the target: dotenv with
BIFROST_ADMIN_USER / BIFROST_ADMIN_PASSWORD, read on the target only).

The requirement kind is ``mcp`` (not the protected ``bifrost*`` component space):
the planner's protection globs match component names, and these are named
``mcp:<client>``; a host can still freeze them with a ``mcp:*`` protected pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from nira.core.bundle import Bundle, Requirement, register
from nira.core.paths import HostPath

if TYPE_CHECKING:
    from nira.core.model import HostConfig, PlanEntry


def load_clients(fleet_path: str | None) -> list[dict[str, Any]]:
    """Parsed ``<fleet>/mcp/*.yaml`` definitions (sorted by file name)."""
    if not fleet_path:
        return []
    out = []
    for f in sorted((Path(fleet_path) / "mcp").glob("*.yaml")):
        data = yaml.safe_load(f.read_text())
        if isinstance(data, dict) and data.get("name"):
            out.append(data)
    return out


def _sym(value: Any) -> Any:
    """``{home: rel}`` -> symbolic ``{{ home }}/rel`` (rendered per run-as user later)."""
    if isinstance(value, dict) and set(value) == {"home"}:
        return str(HostPath.home(str(value["home"])))
    return value


@register
class McpBundle(Bundle):
    name = "mcp"
    description = "bifrost MCP clients from the fleet mcp/*.yaml (disabled unless enabled per host)"
    priority = 60  # after the gateway itself

    @staticmethod
    def _client(raw: dict[str, Any]) -> dict[str, Any]:
        c = {
            "name": raw["name"],
            "transport": raw.get("transport"),
            "url": raw.get("url"),
            "auth": raw.get("auth", "none"),
            "auth_env": list(raw.get("auth_env") or []),
            "tools_to_execute": list(raw.get("tools_to_execute") or ["*"]),
        }
        if raw.get("transport") == "stdio":
            c["command"] = _sym(raw.get("command"))
            c["args"] = list(raw.get("args") or [])
        return c

    def _usable(self, raw: dict[str, Any]) -> str | None:
        """None when plannable, else the reason it is skipped."""
        if raw.get("transport") == "http" and not raw.get("url"):
            return "url unknown (TODO in fleet mcp/ definition)"
        if raw.get("transport") == "stdio" and not _sym(raw.get("command")):
            return "stdio client without command"
        if raw.get("transport") not in ("http", "stdio"):
            return f"unknown transport {raw.get('transport')!r}"
        return None

    def _shared_params(self, host: HostConfig) -> dict[str, Any]:
        params: dict[str, Any] = {"scope": "agent"}
        if host.overrides.get("bifrost_admin_port"):
            params["port"] = int(host.overrides["bifrost_admin_port"])
        if host.overrides.get("mcp_virtual_key"):
            params["virtual_key"] = host.overrides["mcp_virtual_key"]
        for key, param in (("mcp_creds_sops", "creds_sops"), ("mcp_creds_file", "creds_file")):
            if host.overrides.get(key):
                params[param] = host.override_path(key)
                if isinstance(params[param], HostPath):
                    params[param] = str(params[param])
        return params

    def requirements(self, host: HostConfig) -> Iterator[Requirement]:
        enabled = {str(n).lower() for n in host.overrides.get("mcp_enable") or []}
        shared = self._shared_params(host)
        for raw in load_clients(host.fleet_path):
            if self._usable(raw):
                continue
            yield Requirement(
                f"mcp:{raw['name']}",
                "mcp",
                {
                    **shared,
                    "client": self._client(raw),
                    "enabled": raw["name"].lower() in enabled,
                },
            )

    def plan(self, host: HostConfig, facts: dict[str, Any]) -> Iterator[PlanEntry]:
        from nira.core.model import Op, PlanEntry, Status

        yield from super().plan(host, facts)
        for raw in load_clients(host.fleet_path):
            reason = self._usable(raw)
            if reason:
                yield PlanEntry(
                    bundle=self.name,
                    component=f"mcp:{raw['name']}",
                    op=Op.SKIP,
                    status=Status.SKIPPED,
                    detail=reason,
                )
