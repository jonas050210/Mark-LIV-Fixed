"""Sequential multi-application launcher: open, place, and verify one at a time.

Opening several applications and immediately asking the window manager to
arrange all of them tends to race: a window that has not appeared yet cannot
be placed, and a monitor guess made before anything is on screen cannot be
verified. This action removes the race by treating a multi-app request as a
small plan instead of a burst of independent commands — open one application,
wait for its window, place it, verify the placement, retry a bounded number of
times on failure, and only then move to the next step. Progress is reported
through the shared action runtime so the dashboard shows the plan advancing
live, and a step that ultimately fails does not stop the steps behind it.
"""
from __future__ import annotations

import time

from actions.open_app import open_app

_MAX_STEPS = 8
_MAX_ATTEMPTS_PER_STEP = 2  # the first attempt plus one bounded retry
_RETRY_BACKOFF_SECONDS = 1.5
_ALLOWED_STATES = {
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
}

# Phrases open_app uses exclusively when a step did not fully succeed. Kept as
# a plain list rather than a formal return type so open_app's existing string
# contract — and every test that already asserts against it — is untouched;
# this module is the only caller that needs to tell success from failure.
_FAILURE_PREFIXES = (
    "no application name provided",
    "that application name is invalid",
    "unsupported operating system",
    "i cannot pass that to the application",
    "state must be one of",
    "i could not open that link",
    "i could not open '",
    "i could not find an installed application called",
    "matches more than one installed application",
    "i could not start ",
    "i started",
    "failed to open ",
)


def _step_failed(message: str) -> bool:
    text = str(message or "").strip().casefold()
    if not text:
        return True
    if any(text.startswith(prefix) for prefix in _FAILURE_PREFIXES):
        return True
    # "Opened X, but I could not verify the move" / "X is already open, but
    # I could not focus its window" — every partial-placement failure in
    # open_app reads this way; no success message contains the phrase.
    if ", but " in text:
        return True
    return False


def _describe_step(step: dict) -> str:
    app = str(step.get("app_name") or "?")
    parts = []
    monitor = step.get("monitor")
    if monitor not in (None, ""):
        parts.append(f"monitor {monitor}")
    state = str(step.get("state") or "").strip()
    if state:
        parts.append(state)
    detail = f" ({', '.join(parts)})" if parts else ""
    return f"{app}{detail}"


def _validate_steps(raw) -> tuple[list[dict], str]:
    if not isinstance(raw, list) or not raw:
        return [], "Provide at least one step, each with an app_name."
    if len(raw) > _MAX_STEPS:
        return [], f"At most {_MAX_STEPS} steps are supported in one sequence."
    steps = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return [], f"Step {index} must be an object."
        app_name = str(item.get("app_name") or "").strip()
        if not app_name:
            return [], f"Step {index} is missing an app_name."
        state = str(item.get("state") or "").strip().casefold()
        if state and state not in _ALLOWED_STATES:
            return [], f"Step {index}: state must be one of {', '.join(sorted(_ALLOWED_STATES))}."
        steps.append({
            "app_name": app_name[:160],
            "monitor": item.get("monitor"),
            "state": state,
            "arguments": item.get("arguments"),
            "foreground": item.get("foreground"),
        })
    return steps, ""


def _open_app_parameters(step: dict) -> dict:
    params = {"app_name": step["app_name"]}
    if step.get("monitor") not in (None, ""):
        params["monitor"] = step["monitor"]
    if step.get("state"):
        params["state"] = step["state"]
    if step.get("arguments"):
        params["arguments"] = step["arguments"]
    if step.get("foreground") is not None:
        params["foreground"] = step["foreground"]
    return params


def _run_step(step: dict, player, cancel_event) -> tuple[bool, str]:
    """Run one step with a bounded retry, stopping early on cancellation."""
    last_message = ""
    for attempt in range(1, _MAX_ATTEMPTS_PER_STEP + 1):
        if cancel_event is not None and cancel_event.is_set():
            return False, "Cancelled before this step could run."
        try:
            last_message = open_app(_open_app_parameters(step), player=player)
            failed = _step_failed(last_message)
        except Exception as exc:  # a step must never take the whole plan down
            last_message = f"'{step['app_name']}' failed unexpectedly ({type(exc).__name__})."
            failed = True
        if not failed:
            return True, last_message
        if attempt < _MAX_ATTEMPTS_PER_STEP:
            deadline = time.monotonic() + _RETRY_BACKOFF_SECONDS
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return False, last_message
                time.sleep(0.1)
    return False, last_message


def app_sequence(parameters=None, player=None, report_progress=None,
                 cancel_event=None) -> dict:
    p = parameters if isinstance(parameters, dict) else {}
    steps, error = _validate_steps(p.get("steps"))
    if error:
        return {"ok": False, "status": "invalid_parameters", "message": error, "data": {}}

    def _progress(value: int, message: str) -> None:
        if report_progress is not None:
            try:
                report_progress(value, message)
            except Exception:
                pass
        if player:
            try:
                player.write_log(f"[app_sequence] {message}")
            except Exception:
                pass

    plan_lines = [f"{i}. {_describe_step(step)}" for i, step in enumerate(steps, 1)]
    _progress(2, "Plan: " + "; ".join(plan_lines))

    total = len(steps)
    results: list[dict] = []
    cancelled = False
    for index, step in enumerate(steps, 1):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            for remaining in steps[index - 1:]:
                results.append({
                    "app_name": remaining["app_name"], "ok": False,
                    "message": "Skipped: the sequence was cancelled.",
                })
            break

        start_pct = int((index - 1) / total * 90) + 5
        _progress(start_pct, f"Step {index}/{total}: opening {_describe_step(step)}.")
        ok, message = _run_step(step, player, cancel_event)
        results.append({"app_name": step["app_name"], "ok": ok, "message": message})
        done_pct = int(index / total * 90) + 5
        status_word = "done" if ok else "failed"
        _progress(done_pct, f"Step {index}/{total} {status_word}: {message}")

    succeeded = sum(1 for r in results if r["ok"])
    failed = [r for r in results if not r["ok"]]
    lines = [
        f"{'✓' if r['ok'] else '✗'} {r['app_name']}: {r['message']}" for r in results
    ]
    summary = "\n".join(lines)

    if cancelled:
        status, ok = "cancelled", False
        headline = f"Sequence cancelled after {succeeded}/{total} step(s)."
    elif not failed:
        status, ok = "succeeded", True
        headline = f"All {total} step(s) completed."
    elif succeeded:
        status, ok = "partial_failure", False
        headline = f"{succeeded}/{total} step(s) completed; {len(failed)} failed."
    else:
        status, ok = "failed", False
        headline = f"All {total} step(s) failed."

    _progress(100, headline)
    return {
        "ok": ok,
        "status": status,
        "message": f"{headline}\n{summary}",
        "data": {"steps": results},
    }


TOOL = {
    "name": "app_sequence",
    "description": (
        "Opens and arranges several applications for one command, one at a time — "
        "for example 'open Roblox fullscreen on my main monitor, Arena AI on the "
        "left of monitor 2, and YouTube on the right of monitor 2'. Each step is "
        "opened, waited for, placed, and verified before the next one starts, so "
        "windows never fight over focus and a step's result is always genuine, "
        "never assumed. A step that keeps failing is retried once and then "
        "reported without blocking the remaining steps. Prefer this over several "
        "separate open_app calls whenever the user names more than one "
        "application in a single request. Progress is visible live on the "
        "dashboard and the request can be cancelled between steps."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "steps": {
                "type": "ARRAY",
                "maxItems": _MAX_STEPS,
                "description": "Ordered list of applications to open, in the order they should start.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "app_name": {
                            "type": "STRING",
                            "maxLength": 160,
                            "description": "Name of the application, game, or URL to open.",
                        },
                        "monitor": {
                            "type": "STRING",
                            "maxLength": 40,
                            "description": (
                                "Which monitor: a 1-based number ('1', '2'), 'primary'/'main', "
                                "'secondary'/'second', 'left'/'right', or 'monitor 2'."
                            ),
                        },
                        "state": {
                            "type": "STRING",
                            "enum": sorted(_ALLOWED_STATES),
                            "maxLength": 16,
                            "description": "Window state after opening: fullscreen, maximized, minimized, or a snap side.",
                        },
                        "arguments": {
                            "type": "ARRAY",
                            "maxItems": 8,
                            "items": {"type": "STRING", "maxLength": 2048},
                            "description": "Documents or URLs to open with the application.",
                        },
                        "foreground": {
                            "type": "BOOLEAN",
                            "description": "Default true. Set false to open this step in the background.",
                        },
                    },
                    "required": ["app_name"],
                },
            },
        },
        "required": ["steps"],
    },
    "handler": app_sequence,
    "category": "desktop",
    "timeout_seconds": 480.0,
}
