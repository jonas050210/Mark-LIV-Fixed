"""
core/undo.py — one shared undo stack for every action that changes state.

WHY THIS EXISTS
    A voice assistant misunderstands. When it does, "sorry" is not a remedy —
    the file is already in another folder and the brightness is already at 10%.
    Before this module the only recovery was to fix it by hand.

    The alternative — asking "are you sure?" before everything — is worse. Every
    confirmation is a round trip to the model and back, and an assistant that
    checks with you before turning the volume down is one you stop talking to.

    So: act immediately, remember how to reverse it, and let the user say
    "undo". Confirmation is then reserved for the handful of actions that
    genuinely cannot be reversed (see core/confirm.py).

HOW AN ACTION OPTS IN
    Actions do not have to know anything about this file's internals. They
    capture the "before" state and hand back a zero-argument callable:

        from core.undo import push_undo
        old = volume_get()
        volume_set(new)
        push_undo(f"volume → {new}%", lambda: volume_set(old))

    Only the action itself can know that the reverse of "move A to B" is "move
    B to A", which is why this cannot be fully centralised. What IS central is
    the stack, the ordering, the thread safety and the tool the model calls.

COST
    Pushing is a list append behind a lock: microseconds. Nothing in here runs
    until the user actually asks to undo something. This module cannot make the
    assistant slower.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

# How many reversible operations we keep. Ten is roughly "this conversation":
# far enough back to catch a mistake you noticed a few commands later, short
# enough that a closure holding a file's old contents cannot pile up in RAM.
MAX_DEPTH = 10


@dataclass
class _Entry:
    label:   str                    # human sentence, spoken back to the user
    undo:    Callable[[], str]      # returns a short result string
    at:      float = field(default_factory=time.monotonic)


_stack: list[_Entry] = []
_lock = threading.Lock()
_undo_lock = threading.Lock()


class UndoRefused(RuntimeError):
    """The world changed, so applying an undo would be unsafe."""


def refuse(reason: str) -> None:
    raise UndoRefused(str(reason))


def push_undo(label: str, undo_fn: Callable[[], str]) -> None:
    """Record that `label` just happened and `undo_fn()` reverses it.

    Called from action handlers, which run in executor threads — hence the
    lock. Never raises: a broken undo registration must not take down the
    action that actually succeeded."""
    if not callable(undo_fn):
        return
    try:
        with _lock:
            _stack.append(_Entry(label=str(label)[:120], undo=undo_fn))
            # Drop the oldest rather than refusing the newest: the recent past
            # is what people ask to undo.
            while len(_stack) > MAX_DEPTH:
                _stack.pop(0)
    except Exception as e:                                  # pragma: no cover
        print(f"[Undo] push failed ({type(e).__name__}).")


def can_undo() -> bool:
    with _lock:
        return bool(_stack)


def peek() -> str:
    """Label of the operation that `undo_last()` would reverse, or ''."""
    with _lock:
        return _stack[-1].label if _stack else ""


def history() -> list[str]:
    """Most recent first — used by the UI panel and the `undo` tool's list mode."""
    with _lock:
        return [e.label for e in reversed(_stack)]


def undo_last() -> str:
    """Reverse the newest operation, retaining safely-refused entries.

    Only one undo runs at a time. An operation that raises ``UndoRefused`` stays
    in its original stack position so the user can resolve the conflict and try
    again; unexpected failures are consumed because retrying unknown partial
    work could apply the reverse operation twice.
    """
    with _undo_lock:
        with _lock:
            entry = _stack.pop() if _stack else None
            prefix = list(_stack)

        if entry is None:
            return ("There is nothing to undo. I only track things I changed myself — "
                    "files I moved or wrote, and settings I adjusted.")

        try:
            detail = entry.undo() or ""
        except UndoRefused as exc:
            with _lock:
                # New operations may have been pushed while the undo checked the
                # filesystem. Keep those newer than this retained entry.
                if _stack[:len(prefix)] == prefix:
                    _stack.insert(len(prefix), entry)
            return f"Undo refused for '{entry.label}': {exc}"
        except Exception as e:
            return f"Could not undo '{entry.label}' ({type(e).__name__})."

        return f"Undone: {entry.label}." + (f" {detail}" if detail else "")


def clear() -> None:
    """Forget the stack. Called when the app shuts down so closures holding old
    file contents do not outlive the session."""
    with _lock:
        _stack.clear()
