"""The page the user is actually looking at, shared between two launchers.

MARK LIV can put a web page on screen in two different ways, and they do not
know about each other:

* ``actions/open_app.py`` and ``browser_control``'s navigation actions open the
  user's real browser — their profile, their logins, their extensions;
* ``browser_control``'s interactive actions (click, type, read) need a browser
  Playwright can drive, which is a separate window with a separate profile.

If the second one starts blind it lands on ``about:blank``, so "open the BBC"
followed by "click the top story" used to look at an empty page and report that
it could not find the element. This module is the one line of shared state that
prevents that: whichever surface last put a page on screen records it here, and
the automation window resumes from it exactly once.

It holds a URL string and nothing else — no handles, no session objects — so
neither module has to import the other.
"""
from __future__ import annotations

import threading
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_LAST_URL = ""
MAX_URL_CHARS = 4096


def is_web_url(value: object) -> bool:
    """Whether the text is an http(s) address, without normalising it."""
    text = str(value or "").strip()
    if not text or len(text) > MAX_URL_CHARS:
        return False
    try:
        parsed = urlsplit(text)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def note(url: str) -> None:
    """Record the page that was just opened in the user's own browser."""
    # Not truncated to the limit: half a URL is a different page, and resuming
    # the automation window on it would be worse than not resuming at all.
    text = str(url or "").strip()
    if not is_web_url(text):
        return
    global _LAST_URL
    with _LOCK:
        _LAST_URL = text


def peek() -> str:
    """The recorded page, without consuming it."""
    with _LOCK:
        return _LAST_URL


def pop() -> str:
    """The recorded page, consumed.

    Consuming it is what keeps a later automation step on the page the user
    navigated to in the meantime, instead of dragging the window back to the
    first page of the session on every click.
    """
    global _LAST_URL
    with _LOCK:
        url, _LAST_URL = _LAST_URL, ""
        return url


def clear() -> None:
    global _LAST_URL
    with _LOCK:
        _LAST_URL = ""
