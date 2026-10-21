"""Typed host paths and run-as home resolution.

A ``HostPath`` is either relative to the run-as user's home (``HostPath.home``)
or an absolute path that contains no per-user literal (``HostPath.abs``).
Nothing here hardcodes ``/Users/<name>`` or ``/home/<name>``: the home
directory is resolved for the *run-as* user (not ssh_user) via
``getent passwd`` (linux) / ``dscl . -read /Users/<u> NFSHomeDirectory``
(macos).  Resolution is injected (``home_of(user) -> str``) so it is pure and
unit-testable; ``default_home_of`` shells out and is only used at apply time.

``str(HostPath)`` is symbolic (``{{ home }}/rel`` or the absolute path) so it
can live in plan entries and requirement names; render it with
``HostPath.render(home)`` or ``nira.core.render_tpl.resolve_params``.
"""

from __future__ import annotations

import functools
import posixpath
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nira.core.model import HostConfig

HomeOf = Callable[[str], str]

HOME_TOKEN = "{{ home }}"
_USER_LITERAL = re.compile(r"^/(Users|home)(/|$)")
_FORBIDDEN_SUBSTR = ("~", "$HOME", "${", "__HOME__")


class HostPathError(ValueError):
    pass


@dataclass(frozen=True)
class HostPath:
    base: str  # "home" | "abs"
    path: str  # relative path (home) or absolute path (abs)

    def __post_init__(self) -> None:
        if self.base not in ("home", "abs"):
            raise HostPathError("HostPath is only constructible via .home() / .abs()")

    @classmethod
    def home(cls, rel: str = "") -> HostPath:
        rel = rel or ""
        if rel.startswith("/") or any(s in rel for s in _FORBIDDEN_SUBSTR):
            raise HostPathError(f"home-relative path must be plain relative: {rel!r}")
        norm = posixpath.normpath(rel) if rel else ""
        if norm == ".":
            norm = ""
        if norm == ".." or norm.startswith("../"):
            raise HostPathError(f"home-relative path escapes home: {rel!r}")
        return cls("home", norm)

    @classmethod
    def abs(cls, path: str) -> HostPath:
        if not path.startswith("/"):
            raise HostPathError(f"absolute path required: {path!r}")
        if any(s in path for s in _FORBIDDEN_SUBSTR):
            raise HostPathError(f"forbidden home shorthand in {path!r}")
        if _USER_LITERAL.match(path):
            raise HostPathError(f"per-user literal path not allowed (use HostPath.home): {path!r}")
        return cls("abs", path)

    def render(self, home: str) -> str:
        if self.base == "abs":
            return self.path
        return posixpath.join(home, self.path) if self.path else home

    def __truediv__(self, rel: str) -> HostPath:
        if self.base == "home":
            return HostPath.home(posixpath.join(self.path, rel))
        return HostPath.abs(posixpath.join(self.path, rel))

    def __str__(self) -> str:
        if self.base == "abs":
            return self.path
        return f"{HOME_TOKEN}/{self.path}" if self.path else HOME_TOKEN


# ---------------- pure parsing of home lookups ----------------


def parse_getent(output: str, user: str) -> str:
    """Parse ``getent passwd <user>`` output -> home dir."""
    line = output.strip().splitlines()[0] if output.strip() else ""
    fields = line.split(":")
    if len(fields) < 6 or fields[0] != user or not fields[5].startswith("/"):
        raise HostPathError(f"cannot resolve home of {user!r} from getent output")
    return fields[5]


def parse_dscl(output: str, user: str) -> str:
    """Parse ``dscl . -read /Users/<u> NFSHomeDirectory`` output -> home dir."""
    m = re.search(r"NFSHomeDirectory:\s*(\S+)", output)
    if not m or not m.group(1).startswith("/"):
        raise HostPathError(f"cannot resolve home of {user!r} from dscl output")
    return m.group(1)


def lookup_command(os_name: str, user: str) -> str:
    q = shlex.quote(user)
    if os_name == "macos":
        return f"dscl . -read /Users/{q} NFSHomeDirectory"
    return f"getent passwd {q}"


def resolve_home(os_name: str, user: str, run: Callable[[str], str]) -> str:
    """Pure resolver: ``run`` executes a shell command on the target, returns stdout."""
    out = run(lookup_command(os_name, user))
    return parse_dscl(out, user) if os_name == "macos" else parse_getent(out, user)


def default_home_of(host: HostConfig) -> HomeOf:
    """Apply-time resolver: shells out locally or via ssh (cached per user)."""

    def run(cmd: str) -> str:
        argv = ["sh", "-c", cmd]
        if not host.local:
            argv = ["ssh", "-o", "BatchMode=yes", f"{host.ssh_user}@{host.address}", cmd]
        res = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        if res.returncode != 0:
            raise HostPathError(f"home lookup failed on {host.name}: {res.stderr.strip()}")
        return res.stdout

    @functools.cache
    def home_of(user: str) -> str:
        return resolve_home(host.os, user, run)

    return home_of
