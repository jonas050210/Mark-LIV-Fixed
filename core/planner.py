"""Action planner: multi-step goals → grounded tool steps → verified execution.

Two entry points:

- :func:`plan` — decompose a goal into ``[{tool, params, why}]`` without
  running anything. Pure: rules first, optional local-AI fallback, never any
  cloud call (the cloud model does its own chaining via function calls).
- :func:`run_plan` — execute a goal Plan → Execute → Verify: run each step
  through the dispatcher, verify it with :mod:`core.verify`, and stop at the
  first unverified step with an honest message instead of building on a
  success that never happened.

Single-step goals are NOT wrapped in ceremony: when the goal decomposes to
exactly one step, :func:`run_plan` runs it directly (same code path, no
"plan of one" narration).

Grounding rule: a plan may only name tools the dispatcher currently offers
(:meth:`ToolDispatcher.available_tools`). Anything else is dropped before
execution, and an empty plan is reported — never executed.
"""

from __future__ import annotations

import re
from typing import Any

from core import tasks
from core.verify import Verification, verify


# ── rule-based decomposition ─────────────────────────────────────────────────

#: Clause separators for multi-step goals (German + English), in split order.
_STEP_SPLIT_RE = re.compile(
    r"\s*(?:\bund then\b|\band\b|\bdann\b|\s+then\s+|\n|;)\s*",
    re.IGNORECASE,
)

#: (compiled pattern, tool, param builder). First match wins per clause.
#: Builders take the regex match + full clause and return params or None
#: (None = pattern matched the gist but no usable argument — keep looking).
_RULES: list[tuple[re.Pattern, str, Any]] = []


def _rule(pattern: str, tool: str | None = None):
    """Register a clause→tool rule. The tool name defaults to the builder's
    name minus the ``_make_`` prefix; pass `tool` when one tool needs several
    rules (e.g. web_search for both queries and news)."""

    def deco(fn):
        _RULES.append(
            (re.compile(pattern, re.IGNORECASE), tool or fn.__name__[6:], fn))
        return fn

    return deco


@_rule(r"\b(?:open|launch|start|öffne|öffnen|starte|starten)\s+(.+)$")
def _make_open_app(m, clause):
    target = m.group(1).strip().rstrip(".")
    return {"app_name": target} if target else None


@_rule(r"\b(?:close|quit|exit|schließe|schließen|beende|beenden)\s+(.+)$")
def _make_close_app(m, clause):
    target = m.group(1).strip().rstrip(".")
    return {"app_name": target} if target else None


@_rule(r"\b(?:search|google|look up|such|suchen|recherchier\w*|finde heraus)\b\s*(?:for\s+)?(.+)$")
def _make_web_search(m, clause):
    q = m.group(1).strip().rstrip(".")
    return {"query": q} if q else None


@_rule(r"\b(?:news|nachrichten|schlagzeilen|headlines)\b\s*(?:(?:about|über|zu)\s+(.+))?$",
       tool="web_search")
def _make_web_search_news(m, clause):
    topic = (m.group(1) or "").strip().rstrip(".")
    return {"mode": "news", "query": topic or "top news"}


@_rule(r"\b(?:remind|erinner\w*)\b\s*(.*)$")
def _make_reminder(m, clause):
    return _parse_reminder(m.group(1).strip())


def _parse_reminder(text: str) -> dict | None:
    """Parse 'remind ...' into the reminder tool's date/time/message.

    Understands relative times (in 10 minutes / in 2 Stunden), clock times
    (at 5pm / um 17:30 Uhr) and tomorrow/morgen. Returns None when no usable
    time is found — a reminder without a time is not plannable.
    """
    from datetime import datetime as _dt, timedelta as _td

    now = _dt.now()
    target: _dt | None = None

    m = re.search(
        r"\bin\s+(\d+|an?|einer?)\s*"
        r"(minutes?|mins?|minuten?|min\b|hours?|hrs?|h\b|stunden?|std\b)",
        text, re.IGNORECASE)
    if m:
        raw_num = m.group(1).lower()
        num = 1 if raw_num in ("a", "an", "eine", "einer") else int(raw_num)
        unit = m.group(2).lower()
        hours = unit.startswith(("hour", "hr", "h", "stunde", "std"))
        target = now + _td(hours=num if hours else 0,
                           minutes=0 if hours else num)
        text = (text[: m.start()] + " " + text[m.end():])

    if target is None:
        m = re.search(
            r"\b(?:at|um)\s+(\d{1,2})(?::(\d{2}))?\s*"
            r"(am|pm|a\.m\.|p\.m\.|uhr)?\b", text, re.IGNORECASE)
        if m:
            hour, minute = int(m.group(1)), int(m.group(2) or 0)
            suffix = (m.group(3) or "").lower().replace(".", "")
            if suffix == "pm" and hour < 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            target = now.replace(hour=hour % 24, minute=minute % 60,
                                 second=0, microsecond=0)
            if target <= now:
                target += _td(days=1)
            text = (text[: m.start()] + " " + text[m.end():])

    day_shift = 0
    if re.search(r"\b(tomorrow|morgen)\b", text, re.IGNORECASE):
        day_shift = 1
        text = re.sub(r"\b(tomorrow|morgen)\b", " ", text,
                      flags=re.IGNORECASE)
    if target is None:
        if not day_shift:
            return None
        target = (now + _td(days=1)).replace(hour=9, minute=0, second=0,
                                             microsecond=0)
    elif day_shift:
        target = target + _td(days=1)

    msg = re.sub(r"^(me\s+)?(to\s+|mich\s+(daran\s+)?(zu\s+)?)?", "",
                 text.strip(), flags=re.IGNORECASE).strip().rstrip(".")
    msg = re.sub(r"\s+", " ", msg).strip()
    if not msg:
        return None
    return {"date": target.strftime("%Y-%m-%d"),
            "time": target.strftime("%H:%M"), "message": msg}


@_rule(r"\b(?:timer|wecker|countdown)\b\s*(?:(?:for|für|von)\s+)?(.+)$")
def _make_timer(m, clause):
    dur = m.group(1).strip().rstrip(".")
    return {"duration": dur or "5m"}


@_rule(r"\b(?:summariz\w+|zusammenfass\w+|fasse\s+zusammen)\b\s*(.+)?$")
def _make_summarize(m, clause):
    target = (m.group(1) or "").strip().rstrip(".")
    if not target:
        return None  # nothing to summarise - leave it to the local model
    return {"source": target}


@_rule(r"\b(?:take a note|note|notier\w*|merk\w* dir)\b\s*(?:that\s+|dass\s+)?(.+)$")
def _make_save_memory(m, clause):
    text = m.group(1).strip().rstrip(".")
    if not text:
        return None
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "note"
    return {"category": "notes", "key": slug, "value": text}


@_rule(r"\b(?:what did i|do you remember|erinnerst du dich|weiß du noch)\b.*$")
def _make_recall_memory(m, clause):
    return {"query": clause.strip()[:200]}


@_rule(r"\b(?:weather|wetter)\b\s*(?:in\s+|für\s+|for\s+)?(.*)$",
       tool="web_search")
def _make_weather_search(m, clause):
    # No dedicated weather tool exists: a web search answers it.
    place = (m.group(1) or "").strip().rstrip(".")
    german = re.search(r"\bwetter\b|\bfür\b", clause, re.IGNORECASE)
    word = "Wetter" if german else "weather"
    return {"query": f"{word} {place}".strip()}


@_rule(r"\b(?:system status|status|systemstatus|auslastung)\b.*$")
def _make_system_status(m, clause):
    return {}


def decompose(goal: str, tool_names: list[str] | None = None) -> list[dict]:
    """Rule-based decomposition. Returns ``[{tool, params, why}]``.

    Clauses no rule matches are skipped here — :func:`plan` decides whether
    the local-AI fallback gets a chance at the whole goal.
    """
    goal = (goal or "").strip()
    if not goal:
        return []
    # tool_names=None: availability unknown, no grounding. tool_names=[]:
    # known-empty, nothing is plannable.
    known = ({t.lower(): t for t in tool_names}
             if tool_names is not None else None)
    steps: list[dict] = []
    clauses = [c.strip() for c in _STEP_SPLIT_RE.split(goal) if c and c.strip()]
    for clause in clauses[:6]:
        for pattern, tool, builder in _RULES:
            m = pattern.search(clause)
            if not m:
                continue
            try:
                params = builder(m, clause)
            except Exception:
                params = None
            if params is None:
                continue
            if known is not None:
                if tool.lower() not in known:
                    continue  # rule matched but tool unavailable - next rule
                real = known[tool.lower()]
            else:
                real = tool
            steps.append({"tool": real, "params": params, "why": clause[:120]})
            break
    return steps


def _ground_local_steps(raw: list[dict], tool_names: list[str]) -> list[dict]:
    known = {t.lower(): t for t in tool_names}
    steps: list[dict] = []
    for step in raw[:6]:
        tool = str(step.get("tool", "")).strip()
        params = step.get("params", {})
        if tool.lower() in known and isinstance(params, dict):
            steps.append({"tool": known[tool.lower()], "params": params,
                          "why": f"local model: {tool}"})
    return steps


def plan(goal: str, tool_names: list[str] | None = None) -> list[dict]:
    """Decompose `goal` into executable steps.

    Rules first (fast, deterministic). When the rules produce nothing usable
    and a local model is reachable, the goal is offered to it once; its steps
    are grounded against `tool_names` and anything ungrounded is dropped.
    Never raises — worst case is an empty plan.
    """
    if tool_names is not None and not tool_names:
        return []
    tool_names = list(tool_names or [])
    try:
        steps = [s for s in decompose(goal, tool_names or None)
                 if s.get("tool") != "agent_task"]
    except Exception:
        steps = []
    if steps:
        return steps
    # Rules found nothing: one local-model attempt, then give up honestly.
    try:
        from core import local_ai as _lai

        raw = _lai.plan_steps(goal, tool_names)
        if raw:
            # Never plan the planner: a nested agent_task step would recurse
            # run_plan into itself (the local model is the only source that
            # could propose it).
            return [s for s in _ground_local_steps(raw, tool_names)
                    if s.get("tool") != "agent_task"]
    except Exception:
        pass
    return []


# ── Plan → Execute → Verify ──────────────────────────────────────────────────

#: The obvious next step after a step fails *verifiably* (a ground-truth
#: re-check said the effect is not there). Keyed by the failing tool; the
#: builder returns a fallback step, or None when there is nothing sensible to
#: try. This is what "continue automatically" means here: the agent does the
#: obvious thing itself instead of stopping and asking the user to retype it.
def _recover_open_app(params: dict, failed_tool: str) -> dict | None:
    name = str(params.get("app_name") or params.get("name") or "").strip()
    if not name:
        return None
    # It did not start - find out whether it exists at all before saying more.
    return {"tool": "app_inventory", "params": {"action": "where", "query": name},
            "why": f"'{name}' would not start — looking it up in the app inventory"}


def _recover_web_search(params: dict, failed_tool: str) -> dict | None:
    query = str(params.get("query") or "").strip()
    words = query.split()
    if len(words) < 3:
        return None
    shorter = " ".join(words[:3])
    return {"tool": "web_search", "params": {**params, "query": shorter},
            "why": f"the search failed — retrying it as '{shorter}'"}


_RECOVERY = {
    "open_app": _recover_open_app,
    "web_search": _recover_web_search,
    "search": _recover_web_search,
}

#: Tools later steps usually stand on. When one of these fails, the remaining
#: steps are only attempted if they do not act on the same target.
_PREREQUISITE_TOOLS = ("open_app", "close_app", "browser_control")

#: Parameter names that name *what* a step acts on.
_TARGET_KEYS = ("app_name", "name", "query", "path", "file_path", "source",
                "url", "urls", "title", "text", "target", "device")


def _step_target(step: dict) -> set[str]:
    """Normalised values a step acts on, for spotting dependent steps."""
    values: set[str] = set()
    params = step.get("params") or {}
    if not isinstance(params, dict):
        return values
    for key in _TARGET_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            values.add(re.sub(r"[^a-z0-9]+", " ", value.lower()).strip())
    return {v for v in values if v}


def _depends_on(later: dict, failed: dict) -> bool:
    """True when `later` looks like it needs the step that just failed."""
    failed_tool = str(failed.get("tool", "")).lower()
    if failed_tool not in _PREREQUISITE_TOOLS:
        return False
    return bool(_step_target(later) & _step_target(failed))


def run_plan(
    goal: str,
    dispatcher,
    ctx: dict | None = None,
    *,
    tool_names: list[str] | None = None,
    verify_steps: bool = True,
    max_steps: int = 6,
    task=None,
) -> str:
    """Execute `goal` step by step. Returns the spoken-style summary.

    Behaviour, in order:

    - one step → run directly, verify, report (no planning ceremony);
    - several → run in order and verify each;
    - a step that fails a *ground-truth* check gets one bounded recovery
      attempt (see :data:`_RECOVERY`) — the agent keeps going on its own;
    - after that, the remaining steps are still run unless they depend on the
      failed one; a failed step is never reported as success, and the summary
      names exactly what did not work;
    - no plan → say so and name what *was* understood, if anything.

    The whole run reports into the central activity registry (``core/tasks``),
    so the HUD shows progress and can ask it to stop between steps.
    """
    ctx = dict(ctx or {})
    if dispatcher is None:
        return "The action planner is not connected — nothing was run."

    available = tool_names if tool_names is not None else _available(dispatcher)
    steps = plan(goal, available)[: max(1, max_steps)]
    if not steps:
        return (
            f"I could not break that down into anything I can do: '{goal[:120]}'. "
            "Try a single direct command instead."
        )

    total = len(steps)
    if task is None:
        title = goal.strip()[:60] or "Multi-step task"
        task = tasks.start(tasks.KIND_AGENT, title,
                           detail=f"0/{total} steps planned")

    def _cancelled() -> bool:
        try:
            return bool(task.cancel_requested)
        except Exception:
            return False

    def _run_step(step: dict) -> tuple[str, Verification]:
        tool, params = step["tool"], step.get("params", {})
        try:
            result = dispatcher.run(tool, params, ctx) or "Done."
        except Exception as e:  # noqa: BLE001 — dispatcher already stringifies;
            result = f"Tool '{tool}' failed: {e}"  # this is the last-resort net
        if verify_steps:
            verdict = verify(tool, params, result, dispatcher)
        else:
            verdict = Verification(True, "verification skipped", method="none")
        return result, verdict

    done: list[str] = []
    failed_steps: list[tuple[dict, str, str]] = []   # step, detail, recovery note
    ran_after_failure: list[str] = []                # steps run despite a failure
    last_result = ""

    for i, step in enumerate(steps):
        tool = step["tool"]
        if _cancelled():
            task.cancel("Stopped at your request.")
            progress = (f"Finished {len(done)} of {total} steps. " if done else "")
            return (f"{progress}Stopped at your request before step {i + 1} "
                    f"('{tool}'). Nothing else was run.")
        task.update(detail=f"step {i + 1}/{total}: {tool}", progress=i / total)

        result, verdict = _run_step(step)
        last_result = result
        if verdict.ok:
            entry = f"{tool}: {verdict.detail}" if verdict.detail else tool
            (ran_after_failure if failed_steps else done).append(entry)
            continue

        detail = verdict.detail or result[:200]
        recovery_note = ""
        # A verified failure (a re-check measured the missing effect, rather
        # than the tool merely sounding unhappy) is worth one obvious fallback
        # before the plan gives up on that step.
        builder = _RECOVERY.get(str(tool).lower()) if verdict.method == "recheck" else None
        if builder is not None:
            try:
                fallback = builder(step.get("params") or {}, tool)
            except Exception:
                fallback = None
            if fallback and str(fallback.get("tool", "")).lower() in available:
                task.update(detail=f"step {i + 1}/{total}: {fallback['tool']} "
                                   f"(recovery after {tool} failed)")
                _, rverdict = _run_step(fallback)
                # The fallback is *investigation*, not one of the goal's steps:
                # it is reported in the note and never counted as progress.
                detail_text = str(rverdict.detail or fallback["tool"]).rstrip(".")
                if rverdict.ok:
                    recovery_note = f" I checked further: {detail_text}."
                else:
                    recovery_note = (f" I tried {fallback['tool']} as well: "
                                     f"{detail_text or 'no result'}.")
        failed_steps.append((step, detail, recovery_note))

        # Do not abandon independent work just because one step failed, and do
        # not run a step that stands on the failed one (e.g. "close X" after
        # "open X" failed). Either way the failure is reported as a failure.
        remaining = steps[i + 1:]
        if any(_depends_on(later, step) for later in remaining):
            task.fail(error=f"step {i + 1} ('{tool}') failed",
                      detail=detail[:160])
            progress = (
                f"Finished {len(done)} of {total} steps. "
                if total > 1 else ""
            ) + (f"Done: {'; '.join(done)}. " if done else "")
            return (
                f"{progress}Step {i + 1} ('{tool}') did not succeed: {detail}"
                f"{recovery_note} The remaining step(s) need it, so I stopped "
                f"there instead of pretending the rest could work."
            )

    if not failed_steps:
        if total == 1:
            # A single step is a direct command: report the tool's own words.
            task.finish(detail="done")
            return last_result or "Done."
        task.finish(detail=f"all {total} steps done")
        return f"All {total} steps done: " + "; ".join(done) + "."

    worked = len(done) + len(ran_after_failure)
    first_step, first_detail, first_note = failed_steps[0]
    first_index = steps.index(first_step) + 1
    names = ", ".join(str(s[0].get("tool")) for s in failed_steps)
    task.fail(error=f"{len(failed_steps)} step(s) failed: {names}",
              detail=str(first_detail)[:160])

    if total == 1:
        return (f"Step 1 ('{first_step['tool']}') did not succeed: {first_detail}"
                f"{first_note} Nothing else was run.")

    progress = f"Finished {len(done)} of {total} steps. "
    if not ran_after_failure:
        # The failure was on the last step (or nothing may be built on it), so
        # the plan did stop there — same honest wording as before.
        return (
            f"{progress}{'Done: ' + '; '.join(done) + '. ' if done else ''}"
            f"Step {first_index} ('{first_step['tool']}') did not succeed: "
            f"{first_detail}{first_note} I stopped there instead of continuing "
            f"on a step that failed."
        )
    # Steps that did not depend on the failed one were carried on with, so the
    # user gets both halves: what worked anyway, and what still needs them.
    return (
        f"{worked} of {total} steps worked. Step {first_index} "
        f"('{first_step['tool']}') did not succeed: {first_detail}{first_note} "
        f"Carried on with: {'; '.join(ran_after_failure)}. "
        f"The steps still open: {names}."
    )


def _available(dispatcher) -> list[str]:
    try:
        return list(dispatcher.available_tools())
    except Exception:
        return []
