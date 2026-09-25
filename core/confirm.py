"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float
    on_cancel: Optional[Callable[[str], None]] = None


_pending: Optional[_Pending] = None
_expiry_timer: Optional[threading.Timer] = None
_lock = threading.Lock()


class ConfirmationBusyError(RuntimeError):
    """Raised when a second action tries to replace a live confirmation."""


class ConfirmationUnavailableError(RuntimeError):
    """Raised when an irreversible action cannot be shown to the user."""

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def _expire(expected: _Pending) -> None:
    """Actively expire exactly one banner, even if its key is later reused."""
    global _pending, _expiry_timer
    with _lock:
        if _pending is not expected:
            return
        pending = _pending
        _pending = None
        _expiry_timer = None
    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass
    if pending.on_cancel:
        try:
            pending.on_cancel("expired")
        except Exception:
            pass
    _log(f"SYS: Confirmation expired — {pending.title}")


def request(
    key: str,
    title: str,
    detail: str,
    run: Callable[[], str],
    on_cancel: Optional[Callable[[str], None]] = None,
) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud in their own language,
    rather than reading an English string verbatim."""
    global _pending, _expiry_timer

    clean_key = str(key or "").strip()
    clean_title = " ".join(str(title or "").split())[:200]
    clean_detail = " ".join(str(detail or "").split())[:1_000]
    if not clean_key or len(clean_key) > 200 or not clean_title or not callable(run):
        raise ValueError("confirmation metadata is invalid")

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        raise ConfirmationUnavailableError(
            f"I cannot confirm '{clean_title}' because the interface is not available"
        )

    stale: Optional[_Pending] = None
    now = time.monotonic()
    with _lock:
        if _pending is not None and now - _pending.at <= TIMEOUT_SECONDS:
            raise ConfirmationBusyError(
                f"confirmation already pending for '{_pending.title}'"
            )
        stale = _pending
        if _expiry_timer is not None:
            _expiry_timer.cancel()
        pending = _Pending(
            key=clean_key,
            title=clean_title,
            detail=clean_detail,
            run=run,
            at=now,
            on_cancel=on_cancel,
        )
        _pending = pending
        _expiry_timer = threading.Timer(
            TIMEOUT_SECONDS, _expire, args=(pending,)
        )
        _expiry_timer.daemon = True

    if stale is not None and stale.on_cancel:
        try:
            stale.on_cancel("expired")
        except Exception:
            pass

    try:
        _show_cb(clean_title, clean_detail)
    except Exception as e:
        with _lock:
            if _pending is pending:
                _pending = None
                if _expiry_timer is not None:
                    _expiry_timer.cancel()
                    _expiry_timer = None
        raise ConfirmationUnavailableError(
            f"Could not ask for confirmation ({type(e).__name__})"
        ) from e

    with _lock:
        timer = _expiry_timer if _pending is pending else None
    if timer is not None:
        timer.start()
    _log(f"SYS: Awaiting confirmation — {clean_title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {clean_title}. "
        f"Say ONE short sentence in the user's own language telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def resolve(accepted: bool, *, key: str | None = None) -> bool:
    """Resolve only the intended pending confirmation.

    ``key`` is required for remote/action-specific cancellation so cancelling
    one dashboard card can never dismiss another action's confirmation. The
    local HUD may omit it because it can display only the current banner.
    """
    global _pending, _expiry_timer

    with _lock:
        if key is not None and (_pending is None or _pending.key != str(key)):
            return False
        p, _pending = _pending, None
        timer, _expiry_timer = _expiry_timer, None
    if timer is not None:
        timer.cancel()

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return False

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        if p.on_cancel:
            try:
                p.on_cancel("expired")
            except Exception:
                pass
        _log(f"SYS: Confirmation expired — {p.title}")
        return True

    if not accepted:
        if p.on_cancel:
            try:
                p.on_cancel("cancelled")
            except Exception:
                pass
        _log(f"SYS: Cancelled — {p.title}")
        return True

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed ({type(e).__name__}).")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()
    return True


def pending_key() -> str:
    """Identifier of the live confirmation, or an empty string."""
    with _lock:
        if _pending is None or time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.key


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title
