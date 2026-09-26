"""Pure ordering policy for provider batches of function calls."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar


T = TypeVar("T")


def partition_tool_batch(
    calls: Iterable[T], is_read_only: Callable[[T], bool]
) -> list[tuple[bool, list[T]]]:
    """Split calls into ordered read-only groups and one-call write groups.

    Only consecutive read-only calls can be run together. Every call that may
    mutate the PC becomes a group of one, so the original provider ordering is
    preserved around opens, window changes, file mutations, and other side
    effects.
    """
    groups: list[tuple[bool, list[T]]] = []
    pending: list[T] = []
    for call in calls:
        if is_read_only(call):
            pending.append(call)
            continue
        if pending:
            groups.append((True, pending))
            pending = []
        groups.append((False, [call]))
    if pending:
        groups.append((True, pending))
    return groups


__all__ = ["partition_tool_batch"]
