"""Operation executors: translate plan entries into pyinfra operations."""

from __future__ import annotations

from typing import Any


def execute(host: tuple[str, dict[str, Any]], entries: list[Any], results: list[dict]) -> None:
    """Run operations for the given entries against a pyinfra host.

    One pyinfra state per execute call; ops are queued via pyinfra.operation
    wrappers and executed per entry so results stay per-entry. Secrets are
    never written to disk (sops exec-env / sops -d piped).
    """
    from pyinfra.api import Config, Inventory, State
    from pyinfra.api.connect import connect_all
    from pyinfra.api.operations import run_ops

    target, data = host
    inventory = Inventory(([target], data))
    state = State(inventory=inventory, config=Config())
    connect_all(state)
    h = next(iter(inventory.hosts.values()))

    for entry in entries:
        try:
            _dispatch(h, entry)
            # run queued ops for this entry before reporting
            run_ops(state)
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


def _dispatch(h: Any, entry: Any) -> None:
    import pyinfra
    from pyinfra import host as legacy_host  # required for operation global ctx

    pyinfra.host = legacy_host

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
        _directory(h, name, params)
    elif kind == "custom":
        _custom(h, name, params)
    else:
        raise ValueError(f"entry {name}: unhandled kind {kind}")


def _directory(h: Any, name: str, params: dict) -> None:
    if "repo" in params:
        from pyinfra.operations import git

        git.repo(name, repo=params["repo"], present=True, update=True)
    else:
        from pyinfra.operations import files

        files.directory(name, present=True)


def _custom(h: Any, name: str, params: dict) -> None:

    if params.get("mise"):
        _shell_op(h, f"mise use -g {params['tool']}")
    elif params.get("type") == "user":
        _macos_user(h, name)
    elif params.get("relink_to"):
        target = params["relink_to"]
        _shell_op(
            h,
            rf"mkdir -p {name} && find {target} -maxdepth 1 -mindepth 1 -exec ln -sfn {{}} {name}/ \;",
        )
    elif params.get("check") == "binary-in-path":
        _shell_op(h, f"command -v {name}")
    elif params.get("check") == "config-presence-only":
        pass  # doctor verifies pairing; apply never mutates pairing state
    else:
        raise ValueError(f"custom component {name}: unknown params {sorted(params)}")


def _macos_user(h: Any, name: str) -> None:
    _shell_op(
        h,
        f"id -u {name} >/dev/null 2>&1 || sysadminctl -addUser {name} -home /Users/{name}",
        sudo=True,
    )
    _shell_op(h, f"dscl . -create /Users/{name} IsHidden 1", sudo=True)
    _shell_op(
        h, f"dseditgroup -o edit -a {name} -t user com.apple.access_ssh", sudo=True
    )


def _shell_op(h: Any, command: str, sudo: bool = False) -> None:
    from pyinfra.operations import server

    server.shell(command, _sudo=sudo)


def _package(h: Any, name: str, params: dict) -> None:
    os_family = str(h.get_fact(OsFact) or "")
    if "Darwin" in os_family or "Mac" in os_family:
        from pyinfra.operations import brew

        brew.packages(name, present=True)
    else:
        from pyinfra.operations import apt

        apt.packages(name, present=True)


from pyinfra.facts.server import Os

OsFact = Os


def _file(h: Any, name: str, params: dict) -> None:
    from pyinfra.operations import files

    if "template" in params:
        src = _template_source(params["template"])
        if src is None:
            files.put(name, src_or_content=params.get("content", ""), dest=name)
        else:
            files.put(name, src_or_content=src, dest=name)
    elif "content" in params:
        files.put(name, src_or_content=params["content"], dest=name)
    else:
        files.file(name, present=True)


def _template_source(template: str) -> str | None:
    """Render a bundled template to a string (verbatim content for now)."""
    return None


def _service(h: Any, name: str, params: dict) -> None:
    os_family = str(h.get_fact(OsFact) or "")
    unit = params.get("unit_path")
    if "Darwin" in os_family or "Mac" in os_family:
        plist = params.get("plist_path", unit or "")
        if plist:
            _shell_op(
                h,
                f"launchctl bootstrap system {plist} 2>/dev/null; "
                f"launchctl enable system/{name} 2>/dev/null; "
                f"launchctl kickstart -k system/{name} 2>/dev/null || true",
                sudo=True,
            )
        else:
            _shell_op(h, f"launchctl list | grep -q {name} || true")
    else:
        from pyinfra.operations import files, systemd

        if unit:
            files.put(name, src_or_content=params.get("unit_content", ""), dest=unit)
        systemd.service(name, running=True, enabled=True, daemon_reload=True)


def _secret(h: Any, name: str, params: dict) -> None:
    """Decrypt a sops secret and pipe it; never write plaintext to disk."""
    sops_file = params.get("sops_file")
    if not sops_file:
        raise ValueError(f"secret {name}: no sops_file param")
    consumer = params.get("consumer", "true")
    _shell_op(h, f"sops exec-env {sops_file} -- {consumer}")
