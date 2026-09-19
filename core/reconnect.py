"""Voluntary-reconnect signalling, transient-failure classification, and the
pending-command queue for the Gemini Live session loop.

This module is intentionally stdlib-only so the session-loop control flow can
be unit-tested on machines without audio/GUI/cloud dependencies. `main.py`
imports these names; the behaviour contracts live here:

- :class:`ReconnectSignal` unwinds the per-session TaskGroup so the run loop
  rebuilds the Live session. ``keep_context=False`` additionally drops the
  resumption handle (fresh conversation).
- :func:`is_transient_error` recognises transport-level failures (dropped
  socket, reset connection, websocket close 1006/1011/1012/1013, HTTP 5xx /
  429, DNS blips) that deserve a fast reconnect that *keeps* the resumption
  handle, instead of the slow generic back-off path.
- :func:`only_cancelled` / :func:`has_exit_request` classify
  ``(Base)ExceptionGroup`` leaves so a user-requested exit or a pure
  cancellation is never misreported as a crash.
- :class:`PendingCommands` is a small bounded FIFO for user text that arrives
  while no Live session exists (reconnect window, startup). Commands are
  flushed into the next session instead of being dropped.
"""

from __future__ import annotations

import collections
import time
from typing import Any, Iterable, Iterator


# ── voluntary reconnect signal ───────────────────────────────────────────────


class ReconnectSignal(Exception):
    """Raised inside the session TaskGroup to request a session rebuild.

    ``keep_context=True`` keeps the resumption handle so the conversation
    survives the rebuild; ``False`` starts a fresh conversation (used for
    changes the server cannot apply to a resumed session, e.g. voice).
    """

    def __init__(self, keep_context: bool = True) -> None:
        super().__init__("reconnect requested")
        self.keep_context = keep_context


def is_reconnect_signal(exc: BaseException) -> bool:
    """True if `exc` is a ReconnectSignal, or a(n) (Base)ExceptionGroup that
    contains one — possibly nested several layers deep."""
    if isinstance(exc, ReconnectSignal):
        return True
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)):
        return any(is_reconnect_signal(e) for e in exc.exceptions)
    return False


def keep_context_of(exc: BaseException) -> bool:
    """Resolve the desired resumption behaviour for a signal/group.

    When several signals disagree, ``False`` (fresh start) wins: dropping
    context explicitly requested anywhere must not be silently overridden by
    another task that wanted to keep it.
    """
    if isinstance(exc, ReconnectSignal):
        return exc.keep_context
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)):
        return all(keep_context_of(e) for e in exc.exceptions)
    return True


# ── ExceptionGroup leaf classification ───────────────────────────────────────


def iter_leaves(exc: BaseException) -> Iterator[BaseException]:
    """Yield every non-group leaf of `exc`, depth-first, in order."""
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)):
        for sub in exc.exceptions:
            yield from iter_leaves(sub)
    else:
        yield exc


def only_cancelled(exc: BaseException) -> bool:
    """True when every leaf of `exc` is an (asyncio) cancellation.

    A TaskGroup that only ever saw cancellations — e.g. shutdown racing a
    child task — unwound cleanly and must not be logged as a failure.
    """
    import asyncio

    leaves = list(iter_leaves(exc))
    return bool(leaves) and all(
        isinstance(leaf, (asyncio.CancelledError, GeneratorExit)) for leaf in leaves
    )


def has_exit_request(exc: BaseException) -> bool:
    """True when any leaf signals a deliberate process exit.

    Covers ``SystemExit``/``KeyboardInterrupt`` raised by child tasks as well
    as the ``ExceptionGroup("jarvis-exit", ...)`` marker the run loop raises
    for voice/UI-requested shutdowns.
    """
    for leaf in iter_leaves(exc):
        if isinstance(leaf, (SystemExit, KeyboardInterrupt)):
            return True
    if isinstance(exc, (ExceptionGroup, BaseExceptionGroup)) and getattr(
        exc, "message", ""
    ) in ("jarvis-exit",):
        return True
    return any(
        isinstance(sub, (ExceptionGroup, BaseExceptionGroup))
        and getattr(sub, "message", "") == "jarvis-exit"
        for sub in getattr(exc, "exceptions", [])
    )


def leaf_summary(exc: BaseException, _max: int = 5) -> str:
    """One-line-per-leaf summary for logging unhandled TaskGroup failures."""
    lines = []
    for leaf in list(iter_leaves(exc))[:_max]:
        text = f"{type(leaf).__name__}: {leaf}".strip()
        lines.append(text[:300])
    rest = sum(1 for _ in iter_leaves(exc)) - len(lines)
    if rest > 0:
        lines.append(f"... (+{rest} more)")
    return " | ".join(lines) if lines else repr(exc)[:300]


# ── transient network failures: fast reconnect, keep context ────────────────


#: Substrings (lowercased) of transport-level failures. These mean the pipe
#: broke — the conversation server-side is usually still resumable, so the run
#: loop reconnects quickly *with* the resumption handle instead of burning
#: through the slow generic back-off.
TRANSIENT_TOKENS: tuple[str, ...] = (
    # websocket close codes seen on dropped pipes: 1006 abnormal closure,
    # 1011 internal error, 1012 service restart, 1013 try again later
    "1006",
    "1011",
    "1012",
    "1013",
    "abnormal closure",
    "connection reset",
    "connection aborted",
    "connection closed",
    "broken pipe",
    "eof occurred",
    "failed to receive",
    "recv failed",
    "send failed",
    "keepalive",
    "ping timeout",
    "pong timeout",
    "heartbeat timeout",
    "temporarily unavailable",
    "try again later",
    "server disconnected",
    "disconnected",
    # HTTP status texts occasionally surface through the transport layers
    "status 429",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "internal error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "too many requests",
    # DNS / routing blips
    "name resolution",
    "nodename nor servname",
    "temporary failure in name resolution",
    "network is unreachable",
    "no route to host",
)


def is_transient_error(exc: BaseException) -> bool:
    """True when any leaf of `exc` looks like a dropped-pipe network failure.

    Deliberate signals (reconnect / exit / pure cancellation) never count as
    transient — they have their own handling paths.
    """
    if (
        is_reconnect_signal(exc)
        or has_exit_request(exc)
        or only_cancelled(exc)
    ):
        return False
    for leaf in iter_leaves(exc):
        hay = f"{type(leaf).__name__} {leaf}".lower()
        if any(tok in hay for tok in TRANSIENT_TOKENS):
            return True
    return False


def reconnect_delay(
    attempt: int,
    *,
    transient: bool,
    base: float = 1.0,
    cap: float = 60.0,
    transient_cap: float = 10.0,
) -> float:
    """Back-off delay in seconds for reconnect `attempt` (0-based).

    Transient (dropped-pipe) failures reconnect fast with a low cap because
    the server-side session usually survives; anything else uses the slow
    ladder up to `cap`. Pure function of its inputs — safe to unit-test.
    """
    delay = base * (2.0**max(0, attempt))
    return min(delay, transient_cap if transient else cap)


# ── pending user text: never drop a command in the reconnect window ─────────


class PendingCommands:
    """Bounded FIFO for user text that arrives while no session is connected.

    Thread-safe enough for the Qt-thread producer / asyncio-loop consumer
    split: all mutations hold a lock. When full, the oldest entry is dropped —
    the newest command is the one the user still cares about.
    """

    def __init__(self, maxlen: int = 20) -> None:
        import threading

        self._items: collections.deque = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.dropped: int = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def put(self, text: str, source: str = "text") -> bool:
        """Queue `text`. The newest command is the one the user still cares
        about, so a full queue drops the OLDEST entry to make room - and
        returns False to report that a drop happened."""
        text = (text or "").strip()
        if not text:
            return False
        with self._lock:
            dropped = False
            if len(self._items) >= (self._items.maxlen or 0):
                self._items.popleft()
                self.dropped += 1
                dropped = True
            self._items.append({"text": text, "source": source, "at": time.time()})
            return not dropped

    def drain(self) -> list[dict]:
        """Remove and return all queued commands, oldest first."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def peek(self) -> list[dict]:
        with self._lock:
            return list(self._items)
