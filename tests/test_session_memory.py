"""Session (RAM-only) vs. persistent memory separation.

Covers core/session_memory.py and its contract with main.py / the summary
writer: the session adopts the turn list by identity, producers/consumers see
strings, and nothing here ever touches disk. Runnable with pytest or
directly (`python tests/test_session_memory.py`).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session_memory import SessionMemory  # noqa: E402


def test_add_user_and_assistant_shape():
    s = SessionMemory(assistant_name="JARVIS")
    s.add_user("hello there")
    s.add_assistant("General Kenobi.", "JARVIS")
    assert s.turns == ["User: hello there", "JARVIS: General Kenobi."]
    assert len(s) == 2


def test_blank_turns_ignored():
    s = SessionMemory()
    s.add_user("   ")
    s.add_assistant("")
    s.add("  ")
    assert len(s) == 0


def test_max_turns_drops_oldest():
    s = SessionMemory(max_turns=3)
    for i in range(5):
        s.add_user(f"m{i}")
    assert s.turns == ["User: m2", "User: m3", "User: m4"]


def test_recent_returns_copy_oldest_first():
    s = SessionMemory()
    s.add_user("a")
    s.add_user("b")
    s.add_user("c")
    assert s.recent(2) == ["User: b", "User: c"]
    out = s.recent(2)
    out.append("junk")
    assert len(s) == 3


def test_recent_text_bounded():
    s = SessionMemory()
    s.add_user("x" * 100)
    assert s.recent_text(10, max_chars=20) == "x" * 20


def test_user_and_assistant_texts_strip_prefix():
    s = SessionMemory(assistant_name="JARVIS")
    s.add_user("one")
    s.add_assistant("two")
    s.add_user("three")
    assert s.user_texts() == ["one", "three"]
    assert s.assistant_texts() == ["two"]
    assert s.user_texts(1) == ["three"]


def test_clear_empties_in_place():
    s = SessionMemory()
    s.add_user("a")
    ref = s.turns
    s.clear()
    assert s.turns == [] and s.turns is ref


def test_adopts_existing_list_by_identity():
    live_log: list[str] = ["User: already here"]
    s = SessionMemory(live_log)
    assert s.turns is live_log
    s.add_user("next")
    # The original holder sees the same object (SessionSummary relies on this).
    assert live_log == ["User: already here", "User: next"]


def test_session_never_touches_persistent_store():
    # Two sessions are fully independent, and neither writes anywhere: there
    # is simply no disk path in this module (no imports beyond typing).
    import core.session_memory as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    for token in ("open(", "write_text", "Path(", "json.", "sqlite"):
        assert token not in src, token
    a, b = SessionMemory(), SessionMemory()
    a.add_user("private to a")
    assert len(b) == 0
    b.clear()
    assert len(a) == 1


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
