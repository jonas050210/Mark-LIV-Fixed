"""
timer.py — spoken countdown timers ("set a timer for ten minutes").

How this differs from actions/reminder.py
─────────────────────────────────────────
`reminder` writes an entry into the OS task scheduler for a specific date and
time, so it survives a reboot. A cooking timer must not: it has to fire while
the session is alive and then be gone. This action keeps its state in memory
and announces itself through `speak()`.

main.py implements `speak()` with `asyncio.run_coroutine_threadsafe()`, so
calling it from the countdown thread is safe — that is the whole reason the
waiting happens off the handler's thread instead of inside it.

No `eval()`: a duration string is only ever matched against a whitelist and
summed, so model output cannot become code.
"""
from __future__ import annotations

import re
import threading
import time

_MAX_SECONDS = 24 * 60 * 60        # refuse anything longer than a day
_POLL_SECONDS = 0.5                # cancel stays responsive within half a second

_UNITS = {
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}
_TOKEN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*([a-z]*)")
_CLOCK_RE = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")

# Live timers keyed by a short id. Module state on purpose: core/action_loader
# imports this file exactly once per session, so the registry outlives a call.
_TIMERS: dict[str, dict] = {}
_LOCK = threading.Lock()
_COUNTER = 0


def _parse_duration(raw) -> int:
    """Return whole seconds, or 0 when the value cannot be parsed.

    Accepts "90", "90s", "10m", "10 min", "1.5h", "1h30m", "10:30" (mm:ss) and
    "1:00:00" (h:mm:ss). A bare number means minutes — that is what somebody
    shouting "timer, ten" at a kitchen counter almost always wants.
    """
    text = str(raw or "").strip().lower().replace(",", ".")
    if not text:
        return 0

    if _CLOCK_RE.fullmatch(text):
        parts = [int(p) for p in text.split(":")]
        if any(p > 59 for p in parts[-2:]):
            return 0
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]

    total = 0.0
    seen = 0
    pos = 0
    for match in _TOKEN_RE.finditer(text):
        # Only whitespace may sit between two tokens; anything else means the
        # string carries words we do not understand, so refuse it wholesale
        # rather than silently timing something the user did not ask for.
        if match.start() != pos and text[pos:match.start()].strip():
            return 0
        pos = match.end()
        value = float(match.group(1))
        unit = match.group(2)
        if unit:
            if unit not in _UNITS:
                return 0
            total += value * _UNITS[unit]
        else:
            if seen:
                return 0
            total += value * 60
        seen += 1
    if text[pos:].strip() or seen == 0 or total <= 0:
        return 0
    seconds = int(round(total))
    return seconds if 0 < seconds <= _MAX_SECONDS else 0


def _human(seconds: int) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs and not hours:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " ".join(parts) or "0 seconds"


def _log(player, message: str) -> None:
    if player is None:
        return
    try:
        player.write_log(message)
    except Exception:
        pass


def _countdown(timer_id: str, seconds: int, label: str, speak, player) -> None:
    """Worker thread: sleep in slices so a cancel is noticed quickly."""
    deadline = time.monotonic() + seconds
    while True:
        with _LOCK:
            entry = _TIMERS.get(timer_id)
            if entry is None or entry.get("cancelled"):
                # A cancelled timer must leave the registry too, otherwise it
                # stays listed forever and can be "cancelled" again and again.
                _TIMERS.pop(timer_id, None)
                return
            entry["remaining"] = max(0, int(deadline - time.monotonic()))
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(_POLL_SECONDS, left))

    with _LOCK:
        entry = _TIMERS.pop(timer_id, None)
    if entry is None or entry.get("cancelled"):
        return

    message = f"Timer {label} is up." if label else "Your timer is up."
    _log(player, f"TIMER: {message}")
    if speak is None:
        return
    try:
        speak(message)
    except Exception:
        # A dropped announcement must never take the session down with it; the
        # log line above already recorded that the timer fired.
        pass


def _find(wanted: str) -> list[str]:
    """Timer ids matching an id or a label, case-insensitively."""
    needle = str(wanted or "").strip().lower()
    with _LOCK:
        if needle in _TIMERS:
            return [needle]
        return [tid for tid, e in _TIMERS.items()
                if str(e.get("label", "")).strip().lower() == needle] if needle else list(_TIMERS)


def timer_action(parameters: dict, player=None, speak=None) -> str:
    """
    Start, list or cancel a spoken countdown.

    parameters:
        action   : "start" (default) | "list" | "cancel"
        duration : e.g. "10m", "90s", "1h30m", "10"  (start only)
        label    : optional spoken name, also used to cancel by name
    """
    p = parameters if isinstance(parameters, dict) else {}
    action = str(p.get("action") or "start").strip().lower()
    label = str(p.get("label") or "").strip()[:60]

    if action == "list":
        with _LOCK:
            entries = [(tid, dict(e)) for tid, e in _TIMERS.items()]
        if not entries:
            return "No timers are running."
        parts = [f"{e['label'] or tid}: {_human(e.get('remaining', 0))} left"
                 for tid, e in entries]
        return f"{len(entries)} timer(s) running — " + "; ".join(parts) + "."

    if action == "cancel":
        targets = _find(label or str(p.get("duration") or ""))
        if not targets:
            return "There is no matching timer to cancel."
        names = []
        with _LOCK:
            for tid in targets:
                entry = _TIMERS.pop(tid, None)
                if entry is None:
                    continue
                entry["cancelled"] = True      # the worker may already be asleep
                names.append(str(entry.get("label") or tid))
        if not names:
            return "There is no matching timer to cancel."
        _log(player, f"TIMER: cancelled {', '.join(names)}")
        return f"Cancelled {len(names)} timer(s): {', '.join(names)}."

    if action not in ("start", "list", "cancel"):
        return f"Unknown timer action: '{action}'. Available: start, list, cancel."

    seconds = _parse_duration(p.get("duration"))
    if not seconds:
        return ("I could not read that duration. Say something like '10m', "
                "'90s', '1h30m' or '10:30'.")

    global _COUNTER
    with _LOCK:
        _COUNTER += 1
        timer_id = f"t{_COUNTER}"
        _TIMERS[timer_id] = {"label": label, "seconds": seconds,
                             "remaining": seconds, "cancelled": False}

    threading.Thread(
        target=_countdown, args=(timer_id, seconds, label, speak, player),
        daemon=True, name=f"timer-{timer_id}",
    ).start()

    what = f" for {label}" if label else ""
    _log(player, f"TIMER: started {timer_id} — {_human(seconds)}{what}")
    return f"Timer set{what}: {_human(seconds)}."


TOOL = {
    "name": "timer",
    "description": (
        "Starts, lists or cancels a short spoken countdown timer that announces "
        "itself when the time is up. Use this for relative waits such as cooking, "
        "breaks, workouts, tea or parking ('timer for 10 minutes'). For an "
        "appointment at an absolute date or time use the reminder tool instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: start (default), list, cancel.",
            },
            "duration": {
                "type": "STRING",
                "description": "How long to count down, e.g. '10m', '90s', "
                               "'1h30m', '10:30'. A bare number means minutes. "
                               "Maximum 24h. Required for action=start.",
            },
            "label": {
                "type": "STRING",
                "description": "Optional short spoken name for the timer, e.g. "
                               "'pasta'. Also used to cancel it by name.",
            },
        },
        "required": [],
    },
    "handler": timer_action,
}
