"""Operation executors: translate plan entries into pyinfra operations.

Honest results: every entry ends as CHANGED / UNCHANGED / SKIPPED / FAILED,
derived from what pyinfra actually did (per-op success, ``did_change`` or an
explicit change marker, ``state.failed_hosts``), never assumed. Each entry
runs on a fresh pyinfra State so one failure cannot poison the next, and every
entry is VERIFIED afterwards (binary runs, file hash, service active, HTTP
probe); a failed verify makes the entry FAILED.

``build_ops`` is a pure function (entry -> OpSpec list) and the pyinfra
plumbing sits behind ``Backend``, so the logic is testable without any host.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jinja2

from nira.core.bundle import RequirementError, custom_variant
from nira.core.packages import resolve_package
from nira.core.results import Result
from nira.ops import probes
from nira.ops.probes import Ctx, q

CHANGED_MARK = "NIRA_CHANGED"
UNCHANGED_MARK = "NIRA_UNCHANGED"


class ExecError(RuntimeError):
    """An entry cannot be executed as specified (missing content, bad params...)."""


class SkipEntry(Exception):
    """Entry is report-only / manual; nothing to execute."""


@dataclass(frozen=True)
class ExecCtx(Ctx):
    fleet_path: str | None = None
    template_vars: dict[str, str] = field(default_factory=dict)


def ctx_from_data(target: str, data: dict[str, Any]) -> ExecCtx:
    """Build the execution context from pyinfra host data (read defensively)."""
    import getpass

    local = target == "@local"
    return ExecCtx(
        os=str(data.get("os", "linux")),
        ssh_user=str(data.get("ssh_user") or (getpass.getuser() if local else "")),
        agent_user=data.get("agent_user") or None,
        home=data.get("agent_home") or None,
        local=local,
        fleet_path=data.get("fleet_path"),
        template_vars=dict(data.get("template_vars") or {}),
    )


@dataclass
class OpSpec:
    """One pyinfra operation to run: ``op`` is 'module.function'."""

    op: str
    kwargs: dict[str, Any]
    label: str = ""
    marker: bool = False  # shell op that prints NIRA_CHANGED / NIRA_UNCHANGED
    content: str | None = None  # becomes a StringIO ``src`` for files.put


@dataclass
class Built:
    specs: list[OpSpec]
    expected_sha: str | None = None


@dataclass
class Outcome:
    changed: bool = False
    error: str | None = None  # an op failed / host dropped
    verify_error: str | None = None
    verified: bool = False


class Session(Protocol):
    def exists(self, path: str) -> bool: ...
    def run(self, specs: list[OpSpec], verify_cmd: str | None) -> Outcome: ...


class Backend(Protocol):
    def session(self) -> contextlib.AbstractContextManager[Session]: ...


# --------------------------------------------------------------------------- run-as


def run_as(ctx: Ctx, scope: str) -> dict[str, Any]:
    """pyinfra global args for the requested scope."""
    if scope == "admin":
        return {"_sudo": True}
    if scope == "agent" and ctx.agent_user and ctx.agent_user != ctx.ssh_user:
        return {"_sudo": True, "_sudo_user": ctx.agent_user}
    return {}


def _shell(ctx: Ctx, script: str, scope: str, label: str, marker: bool = True) -> OpSpec:
    cmd = f"bash -c {q(probes.PATH_PRELUDE + script)}"
    return OpSpec(
        "server.shell", {"commands": [cmd], **run_as(ctx, scope)}, label=label, marker=marker
    )


def _guarded(check: str, action: str) -> str:
    """Run ``action`` only if ``check`` fails; print a change marker either way."""
    return (
        f"if ( {check} ); then echo {UNCHANGED_MARK}; "
        f"else {{ {action}; }} && echo {CHANGED_MARK}; fi"
    )


# --------------------------------------------------------------------------- content


def _fleet_file(ctx: ExecCtx, rel: str) -> Path:
    if not ctx.fleet_path:
        raise ExecError(f"no fleet path available to resolve {rel!r}")
    root = Path(ctx.fleet_path).resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise ExecError(f"{rel!r} escapes the fleet repo")
    if not path.is_file():
        raise ExecError(f"fleet file not found: {path}")
    return path


def _jinja_env() -> jinja2.Environment:
    return jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)


def resolve_content(name: str, params: dict[str, Any], ctx: ExecCtx) -> str:
    """Resolve file content: inline, fleet src, verbatim agent-config, or template."""
    if "content" in params:
        return str(params["content"])
    if "src" in params:
        return _fleet_file(ctx, params["src"]).read_text()
    if params.get("verbatim"):
        return _fleet_file(ctx, f"agent-config/{Path(name).name}").read_text()
    if "template" in params:
        tpl = params["template"]
        candidates = [f"agent-config/{tpl}", f"agent-config/{re.sub(r'\.j2$', '.tmpl', tpl)}"]
        text = None
        for cand in candidates:
            with contextlib.suppress(ExecError):
                text = _fleet_file(ctx, cand).read_text()
                break
        if text is None:
            raise ExecError(f"template {tpl!r} not found under the fleet agent-config/")
        variables = {"HOME": ctx.agent_home, "SSH_USER": ctx.ssh_user, **ctx.template_vars}
        try:
            text = _jinja_env().from_string(text).render(
                home=ctx.agent_home, user=ctx.agent_user or ctx.ssh_user, **variables
            )
        except jinja2.UndefinedError as e:
            raise ExecError(f"template {tpl!r}: unresolved variable ({e})") from e
        return text
    raise ExecError(f"file {name}: no content source (content/src/verbatim/template)")


def _abs(path: str, ctx: Ctx) -> str:
    return ctx.agent_home + path[1:] if path.startswith("~/") else path


# --------------------------------------------------------------------------- builders


def build_ops(
    kind: str,
    name: str,
    params: dict[str, Any],
    ctx: ExecCtx,
    exists: Any = lambda path: False,
) -> Built:
    """Translate one requirement into pyinfra ops (pure; ``exists`` answers path probes)."""
    builder = {
        "package": _package,
        "file": _file,
        "directory": _directory,
        "service": _service,
        "secret": _secret,
        "custom": _custom,
        "mcp": _mcp,
    }.get(kind)
    if builder is None:
        raise ExecError(f"entry {name}: unhandled kind {kind}")
    scope = probes.effective_scope(kind, name, params, ctx.os)
    return builder(name, params, ctx, scope, exists)


def _mcp(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    from nira.ops.mcp_admin import build_mcp

    return build_mcp(name, params, ctx, scope, exists)


def _package(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    spec = resolve_package(name, ctx.os)
    label = f"package {name}"
    if spec.verify_only or params.get("verify_only") or spec.manager == "external":
        raise SkipEntry(spec.note or "verify-only package; nira does not install it")
    if spec.manager == "mise":
        return Built([_shell(ctx, _mise_install_script(spec), "agent", label)])
    if spec.manager == "apt":
        kw = {"packages": [spec.package], "present": True, "update": True, "cache_time": 86400}
        return Built([OpSpec("apt.packages", {**kw, **run_as(ctx, "admin")}, label=label)])
    if spec.manager in ("brew", "cask"):
        check = probes.probe_snippet("package", name, params, ctx)
        flag = "--cask " if spec.manager == "cask" else ""
        action = f"brew install {flag}{q(spec.package)}"
        return Built([_shell(ctx, _guarded(check, action), "self", label)])
    check = probes.probe_snippet("package", name, params, ctx)
    return Built(
        [_shell(ctx, _guarded(check, "curl -fsSL https://mise.run | sh"), "agent", label)]
    )


def _mise_install_script(spec: Any) -> str:
    """Guarded install of a mise-backed package, run as the run-as user.

    Plain specs use ``mise use -g``. Specs with per-platform URLs or a version
    prefix first merge their table into ``~/.config/mise/config.toml`` with
    ``mise config set`` (toml_edit: other tools/settings stay intact; keys that
    already exist are never overwritten), then ``mise install`` honors it.
    """
    ms, tool = spec.mise, spec.package
    check = f"mise where {q(tool)} >/dev/null 2>&1"
    if not ms.needs_config:
        return _guarded(check, f"mise use -g {q(tool + '@' + ms.version)}")
    if "." in tool:
        raise ExecError(f"mise spec {tool!r}: dots in a table key are not supported")
    cfg = '"$HOME/.config/mise/config.toml"'
    entries = {"version": ms.version}
    if ms.version_prefix:
        entries["version_prefix"] = ms.version_prefix
    for plat, url in ms.platforms.items():
        entries[f"platforms.{plat}.url"] = url
    sets = []
    for key, value in entries.items():
        full = q(f"tools.{tool}.{key}")
        sets.append(
            f"mise config get -f {cfg} {full} >/dev/null 2>&1 || "
            f"mise config set -f {cfg} {full} {q(value)}"
        )
    action = (
        f'mkdir -p "$HOME/.config/mise" && touch {cfg} && '
        + " && ".join(f"{{ {x}; }}" for x in sets)
        + f" && mise install {q(tool)}"
    )
    return _guarded(check, action)


def _file(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    content = resolve_content(name, params, ctx)
    kwargs: dict[str, Any] = {
        "dest": _abs(name, ctx),
        "create_remote_dir": True,
        **run_as(ctx, scope),
    }
    if params.get("mode"):
        kwargs["mode"] = str(params["mode"])
    sha = hashlib.sha256(content.encode()).hexdigest()
    return Built([OpSpec("files.put", kwargs, label=f"file {name}", content=content)], sha)


def _directory(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    dest = _abs(name, ctx)
    glb = run_as(ctx, scope)
    if params.get("repo"):
        spec = OpSpec(
            "git.repo",
            {"src": params["repo"], "dest": dest, "pull": False, **glb},
            label=f"repo {name}",
        )
    else:
        spec = OpSpec(
            "files.directory", {"path": dest, "present": True, **glb}, label=f"dir {name}"
        )
    return Built([spec])


def _service(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    label = f"service {name}"
    if ctx.os == "macos":
        plist = params.get("plist_path") or params.get("unit_path")
        if not plist:
            raise ExecError(f"service {name}: macOS service needs plist_path")
        svc = q(params.get("label", name))
        p = q(plist)
        script = (
            f"L={svc}; P={p}; "
            f'[ -f "$P" ] || {{ echo "plist $P missing" >&2; exit 1; }}; '
            'if launchctl print system/"$L" >/dev/null 2>&1; then '
            'if launchctl print system/"$L" | grep -Eq "state = running|pid = [0-9]+"; '
            f"then echo {UNCHANGED_MARK}; "
            f'else launchctl enable system/"$L" && launchctl kickstart system/"$L" '
            f"&& echo {CHANGED_MARK}; fi; "
            'else launchctl bootstrap system "$P" && launchctl enable system/"$L" '
            f'&& launchctl kickstart system/"$L" && echo {CHANGED_MARK}; fi'
        )
        return Built([_shell(ctx, script, "admin", label)])
    specs: list[OpSpec] = []
    unit = params.get("unit_path")
    if unit:
        specs.extend(_put_unit(name, unit, params, ctx, exists))
    specs.append(
        OpSpec(
            "systemd.service",
            {
                "service": probes.unit_name(name),
                "running": True,
                "enabled": True,
                "daemon_reload": True,
                **run_as(ctx, "admin"),
            },
            label=label,
        )
    )
    return Built(specs)


def _put_unit(name: str, unit: str, params: dict, ctx: ExecCtx, exists: Any) -> list[OpSpec]:
    """Install a unit file, never clobbering an existing one unless adopted.

    Refuses empty content (would blank a live unit) and leaves an existing
    unit untouched unless the requirement sets ``adopt: true``.
    """
    if exists(unit) and not params.get("adopt"):
        return []
    content = params.get("unit_content")
    if not content:
        raise ExecError(f"service {name}: refusing to write empty unit content to {unit}")
    return [
        OpSpec(
            "files.put",
            {"dest": unit, "mode": "0644", **run_as(ctx, "admin")},
            label=f"unit {unit}",
            content=content,
        )
    ]


def _secret(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    """Stage the (encrypted) sops file at an absolute path; plaintext never touches disk."""
    sops_file = params.get("sops_file")
    if not sops_file:
        raise ExecError(f"secret {name}: no sops_file param")
    remote = probes.secret_remote_path(params, ctx)
    specs: list[OpSpec] = []
    if not Path(sops_file).is_absolute():
        local = _fleet_file(ctx, sops_file)
        specs.append(
            OpSpec(
                "files.put",
                {
                    "src": str(local),
                    "dest": remote,
                    "mode": "0600",
                    "create_remote_dir": True,
                    **run_as(ctx, scope),
                },
                label=f"secret {name}",
            )
        )
    consumer = params.get("consumer")
    if consumer:
        specs.append(
            _shell(
                ctx,
                f"sops exec-env {q(remote)} -- {consumer} && echo {CHANGED_MARK}",
                scope,
                f"consume {name}",
            )
        )
    if not specs:
        # absolute path already on the target and no consumer: nothing to stage
        raise SkipEntry("sops file already on target; verify only")
    return Built(specs)


def _custom(name: str, params: dict, ctx: ExecCtx, scope: str, exists: Any) -> Built:
    variant = custom_variant(params)
    label = f"custom {name}"
    if variant == "mise":
        spec = str(params["tool"])
        probes.check_mise_spec(spec)
        script = _guarded(
            f"mise where {q(spec)} >/dev/null 2>&1", f"mise use -g {q(spec)}"
        )
        return Built([_shell(ctx, script, "agent", label)])
    if variant == "type":
        if params["type"] != "user":
            raise ExecError(f"custom {name}: type {params['type']!r} is not implemented")
        n = q(name)
        check = (
            f"id -u {n} >/dev/null 2>&1 && dscl . -read /Users/{n} IsHidden 2>/dev/null"
            f" | grep -q 1 && dseditgroup -o checkmember -m {n} com.apple.access_ssh"
            " >/dev/null 2>&1"
        )
        action = (
            f"{{ id -u {n} >/dev/null 2>&1 || sysadminctl -addUser {n} -home /Users/{n}; }}"
            f" && dscl . -create /Users/{n} IsHidden 1"
            f" && dseditgroup -o edit -a {n} -t user com.apple.access_ssh"
        )
        return Built([_shell(ctx, _guarded(check, action), "admin", label)])
    if variant == "relink_to":
        return Built([_shell(ctx, _relink_script(name, params["relink_to"]), "agent", label)])
    if params["check"] == "binary-in-path":
        action = f'echo "{name} not found on PATH; nira cannot install it" >&2; exit 1'
        script = _guarded(f"command -v {q(name)} >/dev/null 2>&1", action)
        return Built([_shell(ctx, script, "agent", label)])
    raise SkipEntry("report-only check (pairing state is never mutated by nira)")


def _relink_script(dest: str, src: str) -> str:
    """Symlink each top-level entry of src into dest.

    Dotfiles (including .git) are never linked (the glob skips them), and an
    existing real file or directory is left untouched with a note.
    """
    d, s = probes.path_expr(dest), probes.path_expr(src)
    return (
        "c=0; "
        f"[ -d {d} ] || {{ mkdir -p {d} && c=1; }}; "
        f'for e in {s}/*; do [ -e "$e" ] || continue; b=$(basename "$e"); t={d}/"$b"; '
        'if [ -L "$t" ]; then '
        '[ "$(readlink "$t")" = "$e" ] && continue; ln -sfn "$e" "$t" && c=1; '
        'elif [ -e "$t" ]; then echo "left untouched (not a symlink): $t" >&2; '
        'else ln -s "$e" "$t" && c=1; fi; done; '
        f"if [ $c -eq 1 ]; then echo {CHANGED_MARK}; else echo {UNCHANGED_MARK}; fi"
    )


# --------------------------------------------------------------------------- pyinfra backend


class PyinfraBackend:
    """Runs OpSpecs through the pyinfra API, one fresh State per session."""

    def __init__(self, host: tuple[str, dict[str, Any]]) -> None:
        self.target, self.data = host

    @contextlib.contextmanager
    def session(self) -> Iterator[Session]:
        from pyinfra.api import Config, Inventory, State
        from pyinfra.api.connect import connect_all

        inventory = Inventory(([self.target], self.data))
        state = State(inventory=inventory, config=Config())
        try:
            connect_all(state)
        except Exception as exc:
            raise ConnectionError(f"connect failed: {exc}") from exc
        h = next(iter(inventory.hosts.values()))
        if h in state.failed_hosts or h not in state.active_hosts:
            raise ConnectionError(f"host {self.target} unreachable (connection failed)")
        yield _PyinfraSession(state, h)


class _PyinfraSession:
    def __init__(self, state: Any, host: Any) -> None:
        self.state, self.host = state, host

    def exists(self, path: str) -> bool:
        from pyinfra.context import ctx_host, ctx_state
        from pyinfra.facts.files import File

        with ctx_state.use(self.state), ctx_host.use(self.host):
            return self.host.get_fact(File, path=path) is not None

    def run(self, specs: list[OpSpec], verify_cmd: str | None) -> Outcome:
        from importlib import import_module
        from io import StringIO

        from pyinfra.api.operation import add_op
        from pyinfra.api.operations import run_ops
        from pyinfra.operations import server

        out = Outcome()
        metas: list[tuple[OpSpec, Any]] = []
        vmeta = None
        failure: str | None = None
        try:
            for spec in specs:
                module, func = spec.op.split(".")
                fn = getattr(import_module(f"pyinfra.operations.{module}"), func)
                kwargs = dict(spec.kwargs)
                if spec.content is not None:
                    kwargs["src"] = StringIO(spec.content)
                metas.append((spec, add_op(self.state, fn, host=self.host, **kwargs)[self.host]))
            if verify_cmd:
                vmeta = add_op(self.state, server.shell, host=self.host, commands=[verify_cmd])[
                    self.host
                ]
            run_ops(self.state)
        except Exception as exc:  # noqa: BLE001 - pyinfra may raise or silently drop the host
            failure = str(exc)

        errors: list[str] = []
        for spec, meta in metas:
            if not meta.is_complete():
                continue
            if meta.did_error():
                errors.append(f"{spec.label}: {_tail(meta)}")
            elif spec.marker:
                out.changed |= CHANGED_MARK in meta.stdout
            else:
                out.changed |= meta.did_change()
        incomplete = [s.label for s, m in metas if not m.is_complete()]
        if self.host in self.state.failed_hosts and not errors:
            errors.append(failure or "host failed during apply")
        elif incomplete and not errors:
            errors.append(failure or f"operations did not run: {incomplete}")
        elif failure and not errors and not (vmeta is not None and vmeta.is_complete()):
            errors.append(failure)
        if errors:
            out.error = "; ".join(errors)
            return out
        if vmeta is not None:
            if vmeta.is_complete() and vmeta.did_succeed():
                out.verified = True
            else:
                out.verify_error = _tail(vmeta) if vmeta.is_complete() else (failure or "not run")
        return out


def _tail(meta: Any) -> str:
    text = (meta.stderr or meta.stdout or "").strip()
    return text[-300:] if text else "command failed (no output)"


# --------------------------------------------------------------------------- orchestration


def _make_backend(host: tuple[str, dict[str, Any]]) -> Backend:
    return PyinfraBackend(host)


def _result(entry: Any, result: Result, detail: str = "", verified: str = "skipped") -> dict:
    return {
        "bundle": entry.bundle,
        "component": entry.component,
        "result": str(result),
        "detail": detail,
        "verify": verified,
    }


def run_entry(
    entry: Any, ctx: ExecCtx, session: Session, verify: bool = True
) -> dict[str, Any]:
    kind = getattr(entry, "kind", None)
    params = getattr(entry, "params", {}) or {}
    name = entry.component
    try:
        built = build_ops(kind or "", name, params, ctx, session.exists)
    except SkipEntry as exc:
        return _result(entry, Result.SKIPPED, str(exc))
    except (ExecError, RequirementError, OSError, ValueError) as exc:
        return _result(entry, Result.FAILED, str(exc))

    vcmd = None
    if verify:
        scope = probes.effective_scope(kind or "", name, params, ctx.os)
        vcmd = probes.wrap(
            probes.verify_snippet(kind or "", name, params, ctx, built.expected_sha), ctx, scope
        )
    out = session.run(built.specs, vcmd)
    if out.error:
        return _result(entry, Result.FAILED, out.error)
    if out.verify_error:
        return _result(entry, Result.FAILED, f"verify failed: {out.verify_error}", "failed")
    status = Result.CHANGED if out.changed else Result.UNCHANGED
    return _result(entry, status, "", "ok" if out.verified else "skipped")


def execute(
    host: tuple[str, dict[str, Any]],
    entries: list[Any],
    results: list[dict],
    verify: bool = True,
) -> None:
    """Run operations for the given entries against one host, appending results.

    One fresh pyinfra State per entry. Secrets are never written to disk in
    plaintext (the encrypted sops file is staged; decryption is only probed).
    """
    target, data = host
    ctx = ctx_from_data(target, data)
    backend = _make_backend(host)
    unreachable: str | None = None
    for entry in entries:
        if unreachable:
            results.append(_result(entry, Result.FAILED, unreachable))
            continue
        try:
            with backend.session() as session:
                results.append(run_entry(entry, ctx, session, verify=verify))
        except ConnectionError as exc:
            unreachable = str(exc)
            results.append(_result(entry, Result.FAILED, unreachable))
        except Exception as exc:  # noqa: BLE001 - per-entry failure isolation
            results.append(_result(entry, Result.FAILED, f"{type(exc).__name__}: {exc}"))
