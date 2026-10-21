"""Shared fixtures: keep host-home lookups hermetic (never ssh/shell out in tests)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fake_home_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nira.core.render_tpl.default_home_of", lambda host: (lambda user: f"/home/{user}")
    )
