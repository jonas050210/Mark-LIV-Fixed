"""Ephemeral per-run conversation state — the *session*, not the *memory*.

Two stores exist on purpose and must not be confused:

- :class:`SessionMemory` (this module): RAM-only turns of the *current run*.
  Dies with the process, except for the rolling summary that
  :class:`core.session_summary.SessionSummary` persists on shutdown.
- :class:`memory.memory_manager.MemoryManager`: the *persistent* store
  (identity/preferences/notes/... + saved session summaries). Survives
  restarts, curated by the model via save/recall tools.

Turns are plain ``"User: …"`` / ``"<assistant>: …"`` strings — the same shape
the receive loop has always appended — so ``session.turns`` can be passed
wherever the old ``session_log`` list went (summary writer, proactive
context) with zero conversion.
"""

from __future__ import annotations

import threading


class SessionMemory:
    """RAM-only turns of the current run. Never touches disk itself."""

    def __init__(self, turns: list[str] | None = None, max_turns: int = 500,
                 assistant_name: str = "JARVIS") -> None:
        # Adopt an existing list (main.py's turn log) or allocate our own.
        # Adopted lists keep their identity: every existing holder of the
        # object - including SessionSummary, which empties it in place -
        # keeps working untouched.
        self.turns: list[str] = turns if turns is not None else []
        self.max_turns = max(1, int(max_turns))
        self.assistant_name = assistant_name or "JARVIS"
        # Producers run on the loop thread, consumers (proactive checks,
        # plugins, agent plans) on executor threads - guard both sides.
        self._lock = threading.Lock()

    # ── producers ────────────────────────────────────────────────────────────

    def add(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        with self._lock:
            self.turns.append(line)
            while len(self.turns) > self.max_turns:
                self.turns.pop(0)

    def add_user(self, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.add(f"User: {text}")

    def add_assistant(self, text: str, name: str | None = None) -> None:
        text = (text or "").strip()
        if text:
            self.add(f"{name or self.assistant_name}: {text}")

    # ── consumers ────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        with self._lock:
            return len(self.turns)

    def recent(self, n: int = 10) -> list[str]:
        """Last `n` turns, oldest first. Returns a copy."""
        with self._lock:
            return list(self.turns[-max(0, int(n)):]) if n else []

    def recent_text(self, n: int = 10, max_chars: int = 4000) -> str:
        """Last `n` turns joined, bounded to `max_chars`."""
        out = "\n".join(self.recent(n))
        return out[-max_chars:] if len(out) > max_chars else out

    def user_texts(self, n: int = 10) -> list[str]:
        """Last `n` user-side utterances, without the prefix."""
        with self._lock:
            found = [t[6:].strip() for t in self.turns
                     if t.startswith("User: ")]
        return found[-max(0, int(n)):] if n else []

    def assistant_texts(self, n: int = 10) -> list[str]:
        """Last `n` assistant-side utterances, without the prefix."""
        prefix = f"{self.assistant_name}: "
        with self._lock:
            found = [t[len(prefix):].strip() for t in self.turns
                     if t.startswith(prefix)]
        return found[-max(0, int(n)):] if n else []

    def clear(self) -> None:
        """Drop all session turns (fresh conversation, same process)."""
        with self._lock:
            self.turns.clear()
