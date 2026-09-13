"""Operation executors: translate plan entries into pyinfra operations."""

from __future__ import annotations

from typing import Any


def execute(host: tuple[str, dict[str, Any]], entries: list[Any], results: list[dict]) -> None:
    """Run operations for the given entries against a pyinfra host.

    Uses the pyinfra API in two-phase mode; secrets are never written to disk
    (sops exec-env / sops -d piped). Errors are captured per entry.
    """
    from pyinfra import host as host_state
    from pyinfra.api import Config, Inventory, State
    from pyinfra.api.connect import connect_all
    from pyinfra.api.operations import run_ops

    target, data = host
    inventory = Inventory([(target, ["nira"], data)])
    state = State(inventory=inventory, config=Config())
    connect_all(state)

    for entry in entries:
        try:
            _dispatch(state, host_state, entry)
            results.append(
                {
                    "bundle": entry.bundle,
                    "component": entry.component,
                    "result": "changed",
                    "detail": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - per-entry failure isolation
            results.append(
                {
                    "bundle": entry.bundle,
                    "component": entry.component,
                    "result": "failed",
                    "detail": str(exc),
                }
            )
    run_ops(state)


def _dispatch(state: Any, host_state: Any, entry: Any) -> None:
    from pyinfra import host as h

    name = entry.component
    params = getattr(entry, "params", {}) or {}
    kind = getattr(entry, "kind", None)

    if kind == "package":
        _package(h, name, params)
    elif kind == "file":
        _file(h, name, params)
    elif kind == "service":
        _service(h, name, params)
    elif kind == "secret":
        _secret(h, name, params)
    elif kind == "directory":
        h.directory(name, present=True)
    else:
        _service(h, name, params)


def _package(h: Any, name: str, params: dict) -> None:
    os_family = h.fact.os or ""
    if "Debian" in str(os_family) or "Linux" in str(os_family):
        h.apt.packages(name, present=True)
    else:
        h.brew.packages(name, present=True)


def _file(h: Any, name: str, params: dict) -> None:
    if "template" in params:
        h.files.template(
            params["template"],
            name,
            **params.get("context", {}),
        )
    elif "content" in params:
        h.files.put(name, params["content"])
    else:
        h.files.file(name, present=True)


def _service(h: Any, name: str, params: dict) -> None:
    os_family = str(h.fact.os or "")
    unit = params.get("unit_path")
    if "Debian" in os_family or "Linux" in os_family:
        if unit:
            h.files.put(unit, params.get("unit_content", ""), present=True)
        h.systemd.service(name, running=True, enabled=True, daemon_reload=True)
    else:
        # launchd: bootstrap/bootout only, never legacy load/unload
        plist = params.get("plist_path", unit or "")
        if plist:
            h.launchd.service(name, plist=plist, running=True)


def _secret(h: Any, name: str, params: dict) -> None:
    """Decrypt a sops secret and pipe it; never write plaintext to disk."""
    sops_file = params.get("sops_file")
    if not sops_file:
        raise ValueError(f"secret {name}: no sops_file param")
    # executed via sops exec-env so plaintext exists only in env/memory
    h.shell(f'sops exec-env -- {params.get("consumer", "true")}', _env={})
    h.shell(f"sops -d {sops_file} | {params.get('pipe_to', 'cat > /dev/null')}")
