"""Protected-component matching: glob patterns, report-only, never mutated."""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

# Built-in protected names/globs (matched against the whole component name).
BUILTIN_PROTECTED = ("web-services", "brioso-*", "xvfb-orca", "orca.service", "orcad", "bifrost*")


def is_protected(name: str, extra: Iterable[str] = ()) -> bool:
    """True if `name` matches a built-in or host-supplied pattern (fnmatch glob)."""
    return any(fnmatchcase(name, p) for p in (*BUILTIN_PROTECTED, *extra))
