#!/usr/bin/env python3
"""
check_session_memory.py — regression tests for the session-memory loss.

WHAT IT PROVES
    1. A summary that does not get written does NOT delete the conversation.
       (The old code cleared the turn list before the LLM call, so an offline
       model, a timeout or an empty reply cost the whole session, not just the
       summary.)
    2. Every path that ends the process waits for the write: a spoken
       "shutdown", the run loop tearing down, and the window being closed.
    3. The wait is bounded — an assistant that cannot reach the model still
       quits, and says that it could not save.
    4. Nothing is saved twice, and turns that arrive mid-write are kept for the
       next summary instead of vanishing between two batches.
    5. The real chain (SessionSummary → summary → memory_manager →
       long_term.json) puts the session entry on disk.
    6. main.py and ui.py are still wired to all of the above — the bookkeeping
       can be perfect and still save nothing if an exit path forgets to call it.

HOW
    No model, no network, no PyQt6: the LLM call is a callback this script
    controls (fail, return "", hang, be slow), and the memory write is pointed
    at a temp directory so the real long_term.json is never touched.

Run:  python check_session_memory.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

# Legacy Windows consoles cannot encode the symbols below — the same trap
# main.py defuses at startup, and the reason a test report must never be the
# thing that crashes.
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.session_summary import SessionSummary   # noqa: E402

# ── reporting ───────────────────────────────────────────────────────────────

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    return bool(ok)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)), flush=True)


# ── a stand-in for the LLM round trip ───────────────────────────────────────

class FakeModel:
    """What main.py's _write_session_summary does, minus the model.

    `behaviour` is consumed one entry per call: "ok", "empty", "raise", "hang"
    or a float (seconds to take before succeeding).
    """

    def __init__(self, behaviour=("ok",)):
        self.behaviour = list(behaviour)
        self.batches: list[list] = []     # every batch handed to the writer
        self.saved: list[str] = []        # every summary that reached "disk"

    async def write(self, turns: list) -> bool:
        self.batches.append(list(turns))
        mode = self.behaviour.pop(0) if self.behaviour else "ok"
        if isinstance(mode, (int, float)):
            await asyncio.sleep(float(mode))
            mode = "ok"
        if mode == "hang":
            await asyncio.sleep(3600)     # never returns in test time
        if mode == "raise":
            raise RuntimeError("model offline")
        if mode == "empty":
            return False                  # the LLM replied with nothing
        summary = f"Talked about {turns[0].split(':')[-1].strip()}."
        self.saved.append(summary)
        return True


QUIET = lambda _msg: None      # noqa: E731  (test output stays readable)


def conversation(n: int = 4) -> list:
    """A minimal but real-looking session: enough turns to be worth saving."""
    out = []
    for i in range(n):
        out.append(f"User: message {i}")
        out.append(f"MARK: reply {i}")
    return out


# ── 1. the loss this fixes ──────────────────────────────────────────────────

async def test_failure_keeps_the_conversation() -> None:
    section("A failed summary must not delete the session")

    log = conversation()
    model = FakeModel(["raise"])
    s = SessionSummary(log, model.write, logger=QUIET)
    ok = await s.save_now()

    check("writer failure is reported as failure", ok is False)
    check("the turns survive a failed write", s.pending != [],
          f"{len(s.pending)} turns kept")
    check("nothing was written to memory", model.saved == [])
    check("the live log was handed over, not copied",
          log is s.log and log == [], "same object, emptied in place")

    # The retry is the point: the next exit path finishes the job.
    model.behaviour.append("ok")
    ok = await s.save_now()
    check("a later attempt saves what the first one could not", ok is True)
    check("…and writes it exactly once", len(model.saved) == 1, model.saved[0])
    check("the retry buffer is empty afterwards", s.pending == [])
    check("the retry re-sends the same conversation, and only one summary lands",
          model.batches[0] == model.batches[-1] and len(model.saved) == 1,
          f"{len(model.batches)} attempt(s), {len(model.saved)} saved")

    log2 = conversation()
    model2 = FakeModel(["empty"])
    s2 = SessionSummary(log2, model2.write, logger=QUIET)
    check("an empty reply is treated as a failure, not as a summary",
          await s2.save_now() is False and s2.pending != [],
          f"{len(s2.pending)} turns kept")

    log3 = conversation()
    model3 = FakeModel(["raise"])
    s3 = SessionSummary(log3, model3.write, logger=QUIET)

    async def interrupt(_turns):
        raise KeyboardInterrupt            # Ctrl-C is not ours to swallow
    s3._write = interrupt
    propagated = False
    try:
        await s3.save_now()
    except KeyboardInterrupt:
        propagated = True
    check("a hard interrupt still propagates (Ctrl-C quits)", propagated)
    check("…and does not take the conversation with it", s3.pending != [],
          f"{len(s3.pending)} turns kept")


# ── 2. exit paths ───────────────────────────────────────────────────────────

async def test_exit_paths_wait() -> None:
    section("Every way the process ends must wait for the write")

    # The spoken shutdown: the summary starts while the goodbye plays, and
    # flush() is what os._exit() waits for.
    log = conversation()
    model = FakeModel([0.35])              # a realistic few-hundred-ms round trip
    s = SessionSummary(log, model.write, logger=QUIET)
    started = s.start_task()
    check("start_task() kicks the summary off", started is True)
    check("start_task() keeps a reference to the task", s.task is not None)
    check("a second start_task() does not double-summarise", s.start_task() is False)

    t0 = time.monotonic()
    ok = await s.flush(5.0)
    waited = time.monotonic() - t0
    check("flush() waited for the in-flight task", ok is True and waited >= 0.3,
          f"{waited:.2f}s")
    check("…which wrote the session", len(model.saved) == 1)
    check("nothing is left waiting", s.waiting == 0)

    # The window closing / the loop tearing down while the model is unreachable:
    # the budget has to win, but the turns have to survive it.
    log2 = conversation()
    model2 = FakeModel(["hang"])
    s2 = SessionSummary(log2, model2.write, logger=QUIET)
    s2.start_task()
    t0 = time.monotonic()
    ok = await s2.flush(0.4)
    gave_up = time.monotonic() - t0
    check("flush() gives up on a hanging model instead of blocking the exit",
          ok is False and gave_up < 2.0, f"returned after {gave_up:.2f}s")
    check("the turns are still there after the timeout", s2.pending != [],
          f"{len(s2.pending)} turns kept")

    # …and a later exit path (reconnect teardown, window close) can still save
    # them, which is the difference between "lost" and "delayed".
    model2.behaviour.append("ok")
    check("a later flush saves what the timed-out one could not",
          await s2.flush(3.0) is True and len(model2.saved) == 1)

    # A clean start must not stall the exit hook at all.
    model3 = FakeModel()
    s3 = SessionSummary([], model3.write, logger=QUIET)
    t0 = time.monotonic()
    ok = await s3.flush(5.0)
    check("flush() with nothing to save returns immediately",
          ok is True and time.monotonic() - t0 < 0.05 and model3.batches == [])

    # Two turns is a greeting, not a session.
    model4 = FakeModel()
    s4 = SessionSummary(["User: hi", "MARK: hello"], model4.write, logger=QUIET)
    check("a conversation below the minimum is not summarised",
          s4.dirty is False and await s4.save_now() is False and model4.batches == [])
    check("…and does not hold the exit up either", await s4.flush(1.0) is True)


# ── 3. bookkeeping ──────────────────────────────────────────────────────────

async def test_bookkeeping() -> None:
    section("Turns are accounted for, not duplicated or dropped")

    log = conversation(2)                  # 4 turns
    model = FakeModel([0.25])
    s = SessionSummary(log, model.write, logger=QUIET)
    s.start_task()
    await asyncio.sleep(0.05)              # the writer is mid-call…
    log.extend(["User: one more thing", "MARK: noted",   # …and the user keeps
                "User: and another", "MARK: on it"])     #    talking
    await s.flush(3.0)

    check("the first summary covered only the turns it started with",
          len(model.batches[0]) == 4
          and "one more thing" not in " ".join(model.batches[0]))
    check("the turns from during the write were not lost",
          sum(len(b) for b in model.batches) == 8 and s.waiting == 0,
          f"batches={[len(b) for b in model.batches]}")
    # flush() makes a second pass on purpose: on the way out, everything that
    # arrived while the model was thinking has to go in too.
    check("they landed in a second summary, not in the first",
          len(model.saved) == 2 and "one more thing" in model.saved[1],
          f"{len(model.saved)} summaries")

    # Concurrent call sites (run-loop finally + spoken shutdown) write once.
    log2 = conversation()
    model2 = FakeModel([0.2])
    s2 = SessionSummary(log2, model2.write, logger=QUIET)
    await asyncio.gather(s2.save_now(), s2.save_now(), s2.save_now())
    check("concurrent saves are single-flight", len(model2.batches) == 1,
          f"{len(model2.batches)} write(s)")

    # The retry buffer is bounded — a permanently offline model must not turn a
    # long-running session into unbounded memory growth.
    log3 = conversation(20)                # 40 turns
    model3 = FakeModel(["raise", "raise"])
    s3 = SessionSummary(log3, model3.write, logger=QUIET, max_pending=10)
    await s3.save_now()
    log3.extend(conversation(20))
    await s3.save_now()
    check("the retry buffer has a ceiling", len(s3.pending) <= 10,
          f"{len(s3.pending)} turns kept")
    check("the ceiling keeps the NEWEST turns",
          s3.pending[-1].endswith("reply 19"))

    # The logger is how the user learns a session could not be saved.
    lines: list[str] = []
    log4 = conversation()
    model4 = FakeModel(["raise"])
    s4 = SessionSummary(log4, model4.write, logger=lines.append)
    await s4.save_now()
    await s4.flush(1.0)
    check("a failure is reported, not swallowed silently", len(lines) >= 2,
          lines[0][:52] if lines else "no output")


# ── 4. the real memory writer ───────────────────────────────────────────────

async def test_end_to_end(tmp: Path) -> None:
    section("End to end into long_term.json (temp copy)")

    # The real save_session_summary, pointed at a temp file: the user's actual
    # memory store is personal data and must never be touched by a test.
    from memory import memory_manager as mm
    real_path, real_bak = mm.MEMORY_PATH, mm.BACKUP_PATH
    mm.MEMORY_PATH = tmp / "long_term.json"
    mm.BACKUP_PATH = tmp / "long_term.bak"
    try:
        log = conversation()
        model = FakeModel(["raise"])
        s = SessionSummary(log, model.write, logger=QUIET)

        async def write(turns: list) -> bool:
            # Exactly what main.py does: model first, then the real writer.
            if not await model.write(turns):
                return False
            mm.save_session_summary(model.saved[-1], "German")
            return True

        s._write = write
        check("the first attempt fails", await s.save_now() is False)
        check("nothing reached long_term.json", not mm.MEMORY_PATH.exists())

        model.behaviour.append("ok")
        check("the retry succeeds", await s.flush(3.0) is True)
        data = json.loads(mm.MEMORY_PATH.read_text(encoding="utf-8"))
        sessions = data.get("sessions", [])
        check("the session entry is on disk", len(sessions) == 1,
              sessions[0]["summary"] if sessions else "no sessions")
        check("…with the language main.py reads from memory",
              sessions and sessions[0].get("language") == "German")
        check("…and the buffer is empty", s.waiting == 0)
    finally:
        mm.MEMORY_PATH, mm.BACKUP_PATH = real_path, real_bak


# ── 5. the wiring (source level) ────────────────────────────────────────────
#
# Perfect bookkeeping saves nothing if an exit path forgets to call it, and
# main.py cannot be imported here (PyQt6, sounddevice, google.genai), so the
# three call sites are checked as source.

def test_wiring() -> None:
    section("main.py / ui.py still call it on every exit path")

    main_src = (REPO / "main.py").read_text(encoding="utf-8", errors="replace")
    ui_src = (REPO / "ui.py").read_text(encoding="utf-8", errors="replace")

    # shutdown_jarvis: flush before os._exit().
    i = main_src.find("async def _do_shutdown():")
    j = main_src.find("_os._exit(0)", i) if i >= 0 else -1
    between = main_src[i:j] if i >= 0 and j > i else ""
    check("_do_shutdown() exists and ends in os._exit()", i >= 0 and j > i)
    check("shutdown flushes the session BEFORE os._exit()",
          "self._summary.flush(" in between, "awaited between start and exit")
    check("shutdown starts the summary without blocking the goodbye",
          "self._summary.start_task()" in between)

    # The run loop's finally: a tracked task, not a bare create_task().
    check("the run loop uses the tracked task",
          "self._summary.start_task()" in main_src.split("finally:")[-1])
    check("no untracked create_task(self._save_session_summary) is left",
          "create_task(self._save_session_summary" not in main_src)

    # The window closing: the UI hands the exit back to main.py.
    check("main.py registers the window-close hook",
          "self.ui.root.on_quit = self._on_app_closing" in main_src)
    check("the hook flushes on the loop thread",
          "run_coroutine_threadsafe(self._summary.flush(" in main_src)
    check("_RootShim.mainloop() calls the hook after the window is gone",
          "on_quit" in ui_src and "finally:" in ui_src.split("def mainloop(self):")[-1][:400])

    # The old "reset immediately so the next session starts clean" line was the
    # bug: _session_log may only be emptied by SessionSummary, in place.
    check("main.py no longer clears the turn list before the LLM call",
          "reset immediately so the next session starts clean" not in main_src)
    check("…and only ever creates it once",
          main_src.count("self._session_log: list[str] = []") == 1
          and "self._session_log = []" not in main_src.replace(
              "self._session_log: list[str] = []", ""))


async def amain(tmp: Path) -> None:
    await test_failure_keeps_the_conversation()
    await test_exit_paths_wait()
    await test_bookkeeping()
    await test_end_to_end(tmp)


def main() -> int:
    t0 = time.time()
    print("=" * 72)
    print(" MARK LIV — session memory: nothing is lost on the way out")
    print("=" * 72)
    with tempfile.TemporaryDirectory(prefix="markliv-mem-") as td:
        try:
            asyncio.run(amain(Path(td)))
        finally:
            test_wiring()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    failed = [n for n, ok, _d in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f" {passed}/{len(RESULTS)} passed in {time.time() - t0:.1f}s")
    if failed:
        print(" FAILED:")
        for n in failed:
            print(f"   • {n}")
    else:
        print(" A session is only forgotten once it is on disk: a failed, empty")
        print(" or slow summary keeps the conversation, and shutdown, teardown")
        print(" and closing the window all wait for the write.")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
