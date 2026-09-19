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


def run_plan(
    goal: str,
    dispatcher,
    ctx: dict | None = None,
    *,
    tool_names: list[str] | None = None,
    verify_steps: bool = True,
    max_steps: int = 6,
) -> str:
    """Execute `goal` step by step. Returns the spoken-style summary.

    - one step → run directly, verify, report (no planning ceremony);
    - several → run in order, verify each, STOP at the first unverified step
      and say which one failed and what was already done;
    - no plan → say so and name what *was* understood, if anything.
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

    done: list[str] = []
    last_result = ""
    for i, step in enumerate(steps):
        tool, params = step["tool"], step.get("params", {})
        try:
            result = dispatcher.run(tool, params, ctx) or "Done."
        except Exception as e:  # noqa: BLE001 — dispatcher already stringifies;
            result = f"Tool '{tool}' failed: {e}"  # this is the last-resort net
        last_result = result
        if verify_steps:
            verdict: Verification = verify(tool, params, result, dispatcher)
        else:
            verdict = Verification(True, "verification skipped", method="none")
        if verdict.ok:
            done.append(f"{tool}: {verdict.detail}" if verdict.detail else tool)
            continue
        # ── stop: never build on an unverified step ──
        progress = (
            f"Finished {len(done)} of {len(steps)} steps. "
            if len(steps) > 1 else ""
        ) + (f"Done: {'; '.join(done)}. " if done else "")
        return (
            f"{progress}Step {i + 1} ('{tool}') did not succeed: "
            f"{verdict.detail or result[:200]} I stopped there instead of "
            "continuing on a step that failed."
        )

    if len(steps) == 1:
        # A single step is a direct command: report the tool's own words.
        return last_result or "Done."
    return f"All {len(steps)} steps done: " + "; ".join(done) + "."


def _available(dispatcher) -> list[str]:
    try:
        return list(dispatcher.available_tools())
    except Exception:
        return []
