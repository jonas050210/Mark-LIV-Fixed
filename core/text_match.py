"""Shared fuzzy string comparison for application and window matching.

RapidFuzz is used when it is installed because it is one to two orders of
magnitude faster than ``difflib`` and scores partial matches better; an index of
several thousand applications is re-scored on every launcher keystroke, so the
difference is user-visible. ``difflib`` remains the fallback, which keeps the
dependency genuinely optional and the behaviour identical in kind.

Both backends return a ratio in ``0.0 .. 1.0`` so callers never need to know
which one is active.
"""
from __future__ import annotations

import difflib

try:  # optional accelerator
    from rapidfuzz import fuzz as _rapidfuzz

    BACKEND = "rapidfuzz"
except ImportError:  # pragma: no cover - exercised on minimal installations
    _rapidfuzz = None
    BACKEND = "difflib"

_MAX_COMPARE_CHARS = 512


def _clip(value: str) -> str:
    return str(value or "")[:_MAX_COMPARE_CHARS]


def ratio(left: str, right: str) -> float:
    """Similarity of two strings, 0.0 (unrelated) to 1.0 (identical)."""
    first, second = _clip(left), _clip(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.ratio(first, second)) / 100.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def partial_ratio(left: str, right: str) -> float:
    """Similarity tolerant of one string being contained in the other.

    Window titles carry suffixes the user never says — "Inbox (12) - Gmail -
    Google Chrome" for a request of "chrome" — so a plain ratio underrates the
    correct window.
    """
    first, second = _clip(left), _clip(right)
    if not first or not second:
        return 0.0
    if _rapidfuzz is not None:
        return float(_rapidfuzz.partial_ratio(first, second)) / 100.0
    shorter, longer = sorted((first, second), key=len)
    if shorter in longer:
        return 1.0
    return difflib.SequenceMatcher(None, first, second).ratio()


def best_match(query: str, candidates, key=None, minimum: float = 0.0):
    """Highest-scoring candidate above ``minimum``, or ``None``.

    ``key`` extracts the comparable string from each candidate, mirroring the
    signature of ``max`` and ``sorted``.
    """
    best = None
    best_score = minimum
    for candidate in candidates:
        text = key(candidate) if key is not None else candidate
        score = ratio(query, str(text))
        if score > best_score:
            best, best_score = candidate, score
    return best
