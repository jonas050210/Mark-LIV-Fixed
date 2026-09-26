"""Sequential multi-step launcher: open, place, and verify one thing at a time.

Opening several applications and immediately asking the window manager to
arrange all of them tends to race: a window that has not appeared yet cannot
be placed, and a monitor guess made before anything is on screen cannot be
verified. This action removes the race by treating a multi-step request as a
small plan instead of a burst of independent commands — run one step, verify
it actually happened, retry a bounded number of times on failure, and only
then move to the next step. Progress is reported through the shared action
runtime so the dashboard shows the plan advancing live, and a step that
ultimately fails does not stop the steps behind it.

A step is either an application step (open/focus/place an app, the original
and most common case) or a media step (a Spotify Connect command), so a
single ordered plan can mix the two — for example "open Spotify, then play
Bohemian Rhapsody, then open YouTube on the second monitor".
"""
from __future__ import annotations

import time

from actions.media_control import TOOL as _MEDIA_TOOL
from actions.media_control import media_control
from actions.open_app import open_app_result

_MAX_STEPS = 8
_MAX_ATTEMPTS_PER_STEP = 2  # the first attempt plus one bounded retry
_RETRY_BACKOFF_SECONDS = 1.5
_ALLOWED_STATES = {
    "normal", "maximized", "fullscreen", "minimized",
    "left", "right", "top", "bottom",
}

# Media steps reuse media_control's own schema instead of duplicating it, so
# the two can never silently drift apart.
_MEDIA_PROPERTIES = _MEDIA_TOOL["parameters"]["properties"]
_ALLOWED_MEDIA_ACTIONS = set(_MEDIA_PROPERTIES["action"]["enum"])

# media_control returns prose, not a structured result, so a media step's
# success has to be read from its message the same way open_app's used to be.
# This list was built directly from every non-success `return` in
# media_control() (checked against its source, not guessed), so it is a
# closed, exhaustive set rather than an open-ended heuristic.
_MEDIA_FAILURE_PREFIXES = (
    "spotify is not connected",
    "spotify authorization expired",
    "spotify refused playback control",
    "spotify has no active playback device",
    "spotify request failed",
    "spotify connection needs the requests package",
    "spotify token exchange failed",
    "set spotify_client_id",
    "the spotify client id has an invalid format",
    "the spotify redirect uri has an invalid format",
    "tell me what to search for",
    "tell me the position in seconds",
    "spotify found no tracks for",
    "spotify found no track for",
    "volume must be a number",
    "provide a spotify track uri",
    "repeat mode must be",
    "seek position must be",
    "use connect, search, play",
)


def _media_step_failed(message: str) -> bool:
    text = str(message or "").strip().casefold()
    if not text:
        return True
    return any(text.startswith(prefix) for prefix in _MEDIA_FAILURE_PREFIXES)


def _describe_step(step: dict) -> str:
    if step.get("kind") == "media":
        media = step.get("media", {})
        action = str(media.get("action") or "?")
        detail = str(media.get("query") or media.get("track") or media.get("uri") or "")
        return f"Spotify {action}{f' ({detail})' if detail else ''}"
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


def _validate_media_step(index: int, raw_media) -> tuple[dict | None, str]:
    if not isinstance(raw_media, dict):
        return None, f"Step {index}: 'media' must be an object."
    action = str(raw_media.get("action") or "").strip().casefold()
    if not action:
        return None, f"Step {index}: a media step needs an 'action'."
    if action not in _ALLOWED_MEDIA_ACTIONS:
        return None, (
            f"Step {index}: media action must be one of "
            f"{', '.join(sorted(_ALLOWED_MEDIA_ACTIONS))}."
        )
    unknown = sorted(set(raw_media) - set(_MEDIA_PROPERTIES) - {"action"})
    if unknown:
        return None, f"Step {index}: unknown media parameter(s): {', '.join(unknown)}."
    cleaned = {"action": action}
    for key in _MEDIA_PROPERTIES:
        if key != "action" and key in raw_media:
            cleaned[key] = raw_media[key]
    return cleaned, ""


def _validate_steps(raw) -> tuple[list[dict], str]:
    if not isinstance(raw, list) or not raw:
        return [], "Provide at least one step, each with an app_name or a media action."
    if len(raw) > _MAX_STEPS:
        return [], f"At most {_MAX_STEPS} steps are supported in one sequence."
    steps = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return [], f"Step {index} must be an object."
        app_name = str(item.get("app_name") or "").strip()
        raw_media = item.get("media")
        if app_name and raw_media is not None:
            return [], f"Step {index} must be either an app_name step or a media step, not both."
        if raw_media is not None:
            media, error = _validate_media_step(index, raw_media)
            if error:
                return [], error
            steps.append({"kind": "media", "media": media})
            continue
        if not app_name:
            return [], f"Step {index} is missing an app_name or a media action."
        state = str(item.get("state") or "").strip().casefold()
        if state and state not in _ALLOWED_STATES:
            return [], f"Step {index}: state must be one of {', '.join(sorted(_ALLOWED_STATES))}."
        steps.append({
            "kind": "open_app",
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


def _run_with_retry(cancel_event, attempt, error_label: str) -> tuple[bool, str]:
    """Run `attempt()` (returning (ok, message)) with a bounded retry.

    Shared by both step kinds so cancellation-between-attempts, the retry
    count, and the backoff behave identically for an app step and a media
    step.
    """
    last_message = ""
    for attempt_number in range(1, _MAX_ATTEMPTS_PER_STEP + 1):
        if cancel_event is not None and cancel_event.is_set():
            return False, "Cancelled before this step could run."
        try:
            ok, last_message = attempt()
        except Exception as exc:  # a step must never take the whole plan down
            last_message = f"{error_label} failed unexpectedly ({type(exc).__name__})."
            ok = False
        if ok:
            return True, last_message
        if attempt_number < _MAX_ATTEMPTS_PER_STEP:
            deadline = time.monotonic() + _RETRY_BACKOFF_SECONDS
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    return False, last_message
                time.sleep(0.1)
    return False, last_message


def _run_step(step: dict, player, cancel_event) -> tuple[bool, str]:
    """Run one step (open_app or media) with a bounded retry."""
    if step["kind"] == "media":
        action = step["media"].get("action", "?")

        def attempt():
            message = media_control(step["media"], player=player)
            return not _media_step_failed(message), message

        return _run_with_retry(cancel_event, attempt, f"'{action}'")

    def attempt():
        return open_app_result(
            _open_app_parameters(step), player=player, cancel_event=cancel_event,
        )

    return _run_with_retry(cancel_event, attempt, f"'{step['app_name']}'")


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
                    "app_name": _describe_step(remaining), "ok": False,
                    "message": "Skipped: the sequence was cancelled.",
                })
            break

        label = _describe_step(step)
        start_pct = int((index - 1) / total * 90) + 5
        verb = "sending" if step["kind"] == "media" else "opening"
        _progress(start_pct, f"Step {index}/{total}: {verb} {label}.")
        ok, message = _run_step(step, player, cancel_event)
        results.append({"app_name": label, "ok": ok, "message": message})
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
        "Runs several steps for one command, one at a time — for example "
        "'open Roblox fullscreen on my main monitor, Arena AI on the left of "
        "monitor 2, and YouTube on the right of monitor 2', or 'open Spotify, "
        "then play Bohemian Rhapsody, then open Chrome'. Each step is an "
        "application to open and place, or a Spotify media command; each one "
        "is run, waited for, and verified before the next one starts, so "
        "windows never fight over focus and a step's result is always "
        "genuine, never assumed. A step that keeps failing is retried once "
        "and then reported without blocking the remaining steps. Prefer this "
        "over several separate open_app/media_control calls whenever the "
        "user names more than one thing to do in a single request. Progress "
        "is visible live on the dashboard and the request can be cancelled "
        "between steps."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "steps": {
                "type": "ARRAY",
                "maxItems": _MAX_STEPS,
                "description": (
                    "Ordered list of steps to run, in the order they should happen. "
                    "Each step is either an application step (app_name, optionally "
                    "monitor/state/arguments/foreground) or a media step (a nested "
                    "'media' object), never both."
                ),
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
                        "media": {
                            "type": "OBJECT",
                            "description": (
                                "A Spotify Connect command for this step instead of opening "
                                "an application. Mutually exclusive with app_name."
                            ),
                            "properties": _MEDIA_PROPERTIES,
                            "required": ["action"],
                        },
                    },
                },
            },
        },
        "required": ["steps"],
    },
    "handler": app_sequence,
    "category": "desktop",
    "timeout_seconds": 480.0,
}
