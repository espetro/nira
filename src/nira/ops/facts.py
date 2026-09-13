"""Fact collection: `{kind: {name: state}}`.

state is True (satisfied), False (missing) or dict (present but drifted).
Dry mode returns everything missing so tests and plan previews never touch
hosts. pyinfra is imported lazily; in dry mode it is never imported at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nira.core.model import HostConfig

KINDS = ("package", "file", "service", "secret", "directory", "custom")


def _empty() -> dict[str, dict[str, Any]]:
    return {k: {} for k in KINDS}


def collect(host: HostConfig, dry: bool = False) -> dict:
    """Collect host facts. dry=True -> everything reported missing."""
    if dry:
        return _empty()
    from nira.ops.pyinfra_facts import collect_via_pyinfra

    facts = collect_via_pyinfra(host)
    # guarantee all kinds present even if pyinfra collector skipped some
    for k in KINDS:
        facts.setdefault(k, {})
    return facts
