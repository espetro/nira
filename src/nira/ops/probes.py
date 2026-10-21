"""Shell probes: one per requirement kind, shared by fact collection and verify.

A probe is a POSIX shell snippet whose exit status is the verdict:
    0 satisfied, 1 missing, 2 present but drifted/inactive.
Anything else (126/127, sudo refusing, ...) means the probe itself is broken
and is surfaced as an error, never mistaken for "not installed".
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import Any

from nira.core import packages
from nira.core.bundle import RequirementError, custom_variant, default_scope
from nira.core.packages import resolve_package

OK, MISSING, DRIFT = 0, 1, 2
SUDO_FAILED = 99

PATH_PRELUDE = (
    'export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:'
    '/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"; '
)


@dataclass(frozen=True)
class Ctx:
    """Who/where a probe or op runs."""

    os: str
    ssh_user: str
    agent_user: str | None = None
    home: str | None = None  # agent home override
    local: bool = False

    @property
    def run_user(self) -> str:
        return self.agent_user or self.ssh_user

    @property
    def agent_home(self) -> str:
        if self.home:
            return self.home
        if self.local and not self.agent_user:
            return os.path.expanduser("~")
        return f"/Users/{self.run_user}" if self.os == "macos" else f"/home/{self.run_user}"


def ctx_for_host(host: Any) -> Ctx:
    """Ctx from a HostConfig (agent_user is optional on older models)."""
    return Ctx(
        os=host.os,
        ssh_user=host.ssh_user,
        agent_user=getattr(host, "agent_user", None),
        local=bool(host.local),
    )


def q(value: str) -> str:
    return shlex.quote(value)


def path_expr(path: str) -> str:
    """Shell-quote a path, letting a leading ~/ expand to the run-as user's $HOME."""
    if path.startswith("~/"):
        return '"$HOME"/' + q(path[2:])
    return q(path)


def mise_bin(params: dict[str, Any]) -> str:
    """Executable a mise spec provides: explicit ``bin`` or the spec's last component."""
    return packages.mise_bin(str(params["tool"]), params.get("bin"), str(params["tool"]))


def check_mise_spec(spec: str) -> None:
    try:
        packages.check_mise_spec(spec)
    except packages.PackageError as exc:
        raise RequirementError(str(exc)) from exc


def unit_name(name: str) -> str:
    return name if name.endswith((".service", ".timer", ".socket")) else f"{name}.service"


def secret_remote_path(params: dict[str, Any], ctx: Ctx) -> str:
    """Where the encrypted sops file lives on the target.

    Absolute sops_file paths are used verbatim (already on the target); relative
    ones are staged under the agent's ~/.config/nira/secrets by basename.
    """
    sops_file = params.get("sops_file")
    if not sops_file:
        raise RequirementError("secret requires 'sops_file'")
    if os.path.isabs(sops_file):
        return sops_file
    return f"{ctx.agent_home}/.config/nira/secrets/{os.path.basename(sops_file)}"


def _sops_decrypt_check(params: dict[str, Any], ctx: Ctx) -> str:
    target = q(secret_remote_path(params, ctx))
    extract = ""
    if params.get("key"):
        extract = " --extract " + q('["' + params["key"] + '"]')
    return f"sops -d{extract} {target} >/dev/null 2>&1"


def probe_snippet(kind: str, name: str, params: dict[str, Any], ctx: Ctx) -> str:
    """Shell snippet (exit 0 ok / 1 missing / 2 drifted) for one requirement."""
    if kind == "package":
        spec = resolve_package(name, ctx.os)
        if spec.manager == "apt":
            return (
                f"dpkg-query -W -f='${{Status}}' {q(spec.package)} 2>/dev/null"
                " | grep -q 'ok installed'"
            )
        if spec.manager == "brew":
            return f"brew list --formula {q(spec.package)} >/dev/null 2>&1"
        if spec.manager == "cask":
            return f"brew list --cask {q(spec.package)} >/dev/null 2>&1"
        if spec.manager in ("mise", "external"):
            return _mise_package_probe(spec, bool(params.get("verify_only")))
        return 'test -x "$HOME/.local/bin/mise" || command -v mise >/dev/null 2>&1'
    if kind == "file":
        sha = params.get("sha256")
        snippet = f"test -f {path_expr(name)} || exit 1; "
        if sha:
            snippet += (
                f"h=$( (sha256sum {path_expr(name)} 2>/dev/null || shasum -a 256 {path_expr(name)})"
                f" | cut -d' ' -f1); [ \"$h\" = {q(sha)} ] || exit 2"
            )
        return snippet + "exit 0"
    if kind == "directory":
        snippet = f"test -d {path_expr(name)} || exit 1; "
        if params.get("repo"):
            snippet += f"test -d {path_expr(name)}/.git || exit 2; "
        return snippet + "exit 0"
    if kind == "secret":
        enc = q(secret_remote_path(params, ctx))
        return f"test -f {enc} || exit 1; {_sops_decrypt_check(params, ctx)} || exit 2; exit 0"
    if kind == "service":
        return _service_probe(name, params, ctx)
    if kind == "custom":
        return _custom_probe(name, params)
    if kind == "mcp":
        return mcp_probe_snippet(name, params)
    raise RequirementError(f"no probe for kind {kind!r}")


def _mise_package_probe(spec: packages.PackageSpec, verify_only: bool) -> str:
    on_path = f"command -v {q(spec.binary or spec.package)} >/dev/null 2>&1"
    if spec.manager == "external":  # no mise artifact: just look for the binary
        return on_path
    where = f"mise where {q(spec.package)} >/dev/null 2>&1"
    # report-only hosts may carry a hand-installed binary outside mise
    return f"{where} || {on_path}" if (verify_only or spec.verify_only) else where


def _service_probe(name: str, params: dict[str, Any], ctx: Ctx) -> str:
    if ctx.os == "macos":
        label = q(params.get("label", name))
        return (
            f"L={label}; "
            'o=$(launchctl print system/"$L" 2>/dev/null'
            ' || sudo -n launchctl print system/"$L" 2>/dev/null'
            ' || launchctl print "gui/$(id -u)/$L" 2>/dev/null) || exit 1; '
            "echo \"$o\" | grep -Eq 'state = running|pid = [0-9]+' && exit 0; exit 2"
        )
    u = q(unit_name(name))
    return (
        f"systemctl is-active --quiet {u} && exit 0; "
        f"systemctl cat {u} >/dev/null 2>&1 && exit 2; exit 1"
    )


def _custom_probe(name: str, params: dict[str, Any]) -> str:
    variant = custom_variant(params)
    if variant == "mise":
        spec = str(params["tool"])
        check_mise_spec(spec)
        return f"mise where {q(spec)} >/dev/null 2>&1"
    if variant == "type":
        if params["type"] == "user":
            return f"id -u {q(name)} >/dev/null 2>&1"
        raise RequirementError(
            f"custom {name!r}: type {params['type']!r} has no probe; "
            "it cannot be planned until one is implemented"
        )
    if variant == "relink_to":
        dest, src = path_expr(name), path_expr(params["relink_to"])
        return (
            f"[ -d {dest} ] || exit 1; "
            f"for e in {src}/*; do [ -e \"$e\" ] || continue; b=$(basename \"$e\"); "
            f"if [ -L {dest}/\"$b\" ]; then "
            f'[ "$(readlink {dest}/"$b")" = "$e" ] || exit 2; '
            f"elif [ -e {dest}/\"$b\" ]; then continue; else exit 2; fi; done; exit 0"
        )
    if params["check"] == "binary-in-path":
        return f"command -v {q(name)} >/dev/null 2>&1"
    # config-presence-only: orca pairing state
    return 'test -d "$HOME/.config/orca"'


def verify_snippet(
    kind: str, name: str, params: dict[str, Any], ctx: Ctx, expected_sha: str | None = None
) -> str:
    """Stricter post-apply check: the probe plus a behavioural check where possible."""
    p = dict(params)
    if expected_sha and kind == "file":
        p["sha256"] = expected_sha
    base = probe_snippet(kind, name, p, ctx)
    parts = [f"( {base} ) || exit 1"]
    if kind == "package":
        binary = params.get("bin") or resolve_package(name, ctx.os).binary
        if binary and not name.startswith("cask:"):
            parts.append(f"command -v {q(binary)} >/dev/null 2>&1 || exit 1")
    elif kind == "custom" and custom_variant(params) == "mise":
        binary = mise_bin(params)
        parts.append(f"{q(binary)} --version >/dev/null 2>&1 || exit 1")
    elif kind == "service" and params.get("url"):
        parts.append(
            f"ok=1; for i in 1 2 3 4 5 6 7 8; do curl -fsS --max-time 5 {q(params['url'])} "
            ">/dev/null 2>&1 && ok=0 && break; sleep 1; done; [ $ok -eq 0 ] || exit 1"
        )
    body = "; ".join(parts)
    if kind == "service":  # a service may need a moment after (re)start
        return f"for i in 1 2 3 4 5 6 7 8; do ( {body}; exit 0 ) && exit 0; sleep 1; done; exit 1"
    return f"{body}; exit 0"


def effective_scope(kind: str, name: str, params: dict[str, Any], os_name: str) -> str:
    """'admin' (root via sudo), 'agent' (the agent user) or 'self' (the ssh user, no sudo)."""
    if "scope" in params:
        return params["scope"]
    if kind == "package":
        manager = resolve_package(name, os_name).manager
        return {"apt": "admin", "mise-installer": "agent", "mise": "agent"}.get(manager, "self")
    return default_scope(kind, params)


def wrap(snippet: str, ctx: Ctx, scope: str) -> str:
    """Wrap a snippet to run under bash, as the agent user for agent scope."""
    full = f"bash -c {q(PATH_PRELUDE + snippet)}"
    if scope == "agent" and ctx.agent_user and ctx.agent_user != ctx.ssh_user:
        u = q(ctx.agent_user)
        return (
            f"{{ sudo -n -u {u} true || exit {SUDO_FAILED}; }} && "
            f"sudo -n -u {u} -H {full}"
        )
    return full


def build_script(items: list[tuple[int, str]]) -> str:
    """Batch wrapped probes into one script; each prints ``__NIRA__ <idx> <rc>``."""
    lines = []
    for idx, cmd in items:
        lines.append(f'( {cmd} ) </dev/null >/dev/null 2>&1; echo "__NIRA__ {idx} $?"')
    return "\n".join(lines) + "\n"


_LINE = re.compile(r"^__NIRA__ (\d+) (\d+)$")


def parse_results(output: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for line in output.splitlines():
        m = _LINE.match(line.strip())
        if m:
            out[int(m.group(1))] = int(m.group(2))
    return out


# ---------------------------------------------------------------- mcp (bifrost admin API)


def mcp_probe_snippet(name: str, params: dict[str, Any]) -> str:
    """exit 0 client exists with the expected disabled state, 1 missing, 2 drifted, 4 API error."""
    from nira.ops.mcp_admin import helper_command

    return helper_command("probe", params)
