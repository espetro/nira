"""Apply result vocabulary shared by executor, CLI and renderers."""

from __future__ import annotations

from enum import StrEnum


class Result(StrEnum):
    OK = "ok"  # nothing to do / report-only / dry-run
    CHANGED = "changed"  # op ran and changed the host
    UNCHANGED = "unchanged"  # op ran, host already satisfied it
    SKIPPED = "skipped"
    FAILED = "failed"


# Anything outside this set (including unknown strings) counts as a failure.
SUCCESS = frozenset({Result.OK, Result.CHANGED, Result.UNCHANGED, Result.SKIPPED})


def is_success(value: object) -> bool:
    try:
        return Result(str(value).lower()) in SUCCESS
    except ValueError:
        return False
