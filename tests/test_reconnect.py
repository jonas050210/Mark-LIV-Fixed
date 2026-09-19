"""Reconnect signalling, transient classification, pending commands.

Covers core/reconnect.py — the stdlib-only control flow behind the Live
session loop's rebuild behaviour. Runnable with pytest or directly
(`python tests/test_reconnect.py`).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.reconnect import (  # noqa: E402
    PendingCommands,
    ReconnectSignal,
    has_exit_request,
    is_reconnect_signal,
    is_transient_error,
    keep_context_of,
    leaf_summary,
    only_cancelled,
    reconnect_delay,
)


def test_signal_detected_directly():
    assert is_reconnect_signal(ReconnectSignal())
    assert is_reconnect_signal(ReconnectSignal(keep_context=False))
    assert not is_reconnect_signal(ValueError("x"))


def test_signal_detected_nested_in_groups():
    inner = ExceptionGroup("g", [ValueError("a"), ReconnectSignal()])
    outer = BaseExceptionGroup("bg", [inner, RuntimeError("b")])
    assert is_reconnect_signal(outer)
    assert not is_reconnect_signal(ExceptionGroup("g", [ValueError("a")]))


def test_keep_context_false_wins_on_disagreement():
    sig = ExceptionGroup(
        "g", [ReconnectSignal(keep_context=True), ReconnectSignal(keep_context=False)]
    )
    assert keep_context_of(sig) is False
    assert keep_context_of(ReconnectSignal()) is True
    assert keep_context_of(ExceptionGroup("g", [ReconnectSignal()])) is True
    # Unknown shapes must never silently wipe the conversation.
    assert keep_context_of(ValueError("x")) is True


def test_only_cancelled():
    assert only_cancelled(asyncio.CancelledError())
    grp = BaseExceptionGroup(
        "g", [asyncio.CancelledError(), asyncio.CancelledError()]
    )
    assert only_cancelled(grp)
    mixed = BaseExceptionGroup(
        "g", [asyncio.CancelledError(), ValueError("real")]
    )
    assert not only_cancelled(mixed)
    assert not only_cancelled(ValueError("x"))


def test_has_exit_request():
    assert has_exit_request(SystemExit(0))
    assert has_exit_request(KeyboardInterrupt())
    grp = BaseExceptionGroup("g", [ValueError("a"), SystemExit(1)])
    assert has_exit_request(grp)
    assert has_exit_request(ExceptionGroup("jarvis-exit", [RuntimeError("q")]))
    assert not has_exit_request(ValueError("x"))
    assert not has_exit_request(asyncio.CancelledError())


def test_signals_are_never_transient():
    assert not is_transient_error(ReconnectSignal())
    assert not is_transient_error(asyncio.CancelledError())
    assert not is_transient_error(SystemExit(0))


def test_transient_tokens():
    for msg in [
        "WebSocket closed with code 1006",
        "1011 internal error",
        "1012 service restart, try again later",
        "Connection reset by peer",
        "Broken pipe",
        "keepalive ping timeout",
        "server disconnected",
        "HTTP 503 Service Unavailable",
        "status 429 too many requests",
        "Temporary failure in name resolution",
        "network is unreachable",
    ]:
        assert is_transient_error(ConnectionError(msg)), msg
    # Grouped leaves count too.
    grp = ExceptionGroup("g", [ValueError("ok"), OSError("recv failed")])
    assert is_transient_error(grp)


def test_not_transient():
    for msg in [
        "API key not valid",
        "setup refused: unknown field",
        "ValueError: bad argument",
        "User asked to stop",
    ]:
        assert not is_transient_error(RuntimeError(msg)), msg


def test_reconnect_delay_caps():
    assert reconnect_delay(0, transient=True) == 1.0
    assert reconnect_delay(1, transient=True) == 2.0
    assert reconnect_delay(10, transient=True) == 10.0  # transient cap
    assert reconnect_delay(0, transient=False) == 1.0
    assert reconnect_delay(10, transient=False) == 60.0  # slow cap
    assert reconnect_delay(-3, transient=True) == 1.0


def test_leaf_summary_names_leaves():
    grp = ExceptionGroup("g", [ValueError("bad"), KeyError("k")])
    s = leaf_summary(grp)
    assert "ValueError" in s and "bad" in s
    assert "KeyError" in s


def test_pending_commands_fifo_and_cap():
    q = PendingCommands(maxlen=3)
    assert len(q) == 0
    assert q.put("one") and q.put("two")
    assert q.put("  ") is False  # blank text is not queued
    assert len(q) == 2
    assert [i["text"] for i in q.drain()] == ["one", "two"]
    assert len(q) == 0
    assert q.drain() == []


def test_pending_commands_drops_oldest_when_full():
    q = PendingCommands(maxlen=2)
    assert q.put("one")
    assert q.put("two")
    assert q.put("three") is False  # full: oldest dropped
    assert q.dropped == 1
    assert [i["text"] for i in q.peek()] == ["two", "three"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {fn.__name__}: {e!r}")
        else:
            print(f"  [PASS] {fn.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
