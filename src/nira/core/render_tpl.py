"""Template / param rendering for host-specific values.

Only ``{{ home }}`` (run-as user's home) and ``{{ user }}`` (run-as user) are
exposed; any other variable raises (``StrictUndefined``).

For the executor (B-B):
    ctx = HostCtx.for_host(host, scope)          # scope: "agent" | "admin"
    text = render_template("REMOTE.md.j2", ctx)  # searches ctx.template_dirs
    path = render_string(entry.component, ctx)   # "{{ home }}/.config/x" -> abs path
    params = resolve_params(entry.params, ctx)   # renders every str in params (recursively)
Scope comes from ``Requirement.params["scope"]`` (default "admin").
``ctx`` takes an injected ``home_of(user)``; the default shells out
(see nira.core.paths.default_home_of) so tests never need ssh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from nira.core.paths import HomeOf, HostPath, default_home_of

if TYPE_CHECKING:
    from nira.core.model import HostConfig

PACKAGE_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


@dataclass
class HostCtx:
    user: str
    home_of: HomeOf
    template_dirs: list[Path] = field(default_factory=lambda: [PACKAGE_TEMPLATES])
    extra_vars: dict[str, str] = field(default_factory=dict)

    @classmethod
    def for_host(
        cls,
        host: HostConfig,
        scope: str = "admin",
        home_of: HomeOf | None = None,
        template_dirs: list[Path] | None = None,
    ) -> HostCtx:
        from nira.core.model import run_as

        ctx = cls(
            user=run_as(host, scope),
            home_of=home_of or default_home_of(host),
            extra_vars=dict(getattr(host, "template_vars", None) or {}),
        )
        if template_dirs is not None:
            ctx.template_dirs = list(template_dirs)
        return ctx

    @property
    def home(self) -> str:
        return self.home_of(self.user)

    def vars(self) -> dict[str, str]:
        return {**self.extra_vars, "home": self.home, "user": self.user}


def _env(loader: FileSystemLoader | None = None) -> Environment:
    return Environment(
        loader=loader,
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_string(s: str, ctx: HostCtx) -> str:
    if "{{" not in s and "{%" not in s:
        return s
    return _env().from_string(s).render(**ctx.vars())


def render_text(text: str, ctx: HostCtx, **extra: str) -> str:
    """Strictly render template text; undefined variables raise."""
    return _env().from_string(text).render(**ctx.vars(), **extra)


def render_template(name: str, ctx: HostCtx) -> str:
    loader = FileSystemLoader([str(p) for p in ctx.template_dirs])
    return _env(loader).get_template(name).render(**ctx.vars())


def resolve_params(params: Any, ctx: HostCtx) -> Any:
    """Recursively render strings (and HostPaths) in a params structure."""
    if isinstance(params, str):
        return render_string(params, ctx)
    if isinstance(params, HostPath):
        return params.render(ctx.home)
    if isinstance(params, dict):
        return {k: resolve_params(v, ctx) for k, v in params.items()}
    if isinstance(params, list | tuple):
        return type(params)(resolve_params(v, ctx) for v in params)
    return params
